"""oimimo 生命周期内的串行同步任务管理器。"""

import atexit
import queue
import random
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import browser, collector
from .profile_lock import ProfileLock
from . import status_store
from .sync_service import upsert_order


class SyncManager:
    def __init__(self):
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._active = None
        self._pending = {}
        self._last_login_window = None
        self._next_auto_sync = None
        policy = status_store.get_policy_state()
        self._sync_paused = policy['pause_reason'] or None
        try:
            self._retry_attempt = max(0, min(int(policy['retry_attempt']), 8))
        except (TypeError, ValueError):
            self._retry_attempt = 0
        self._retry_at = None
        if policy['retry_after']:
            try:
                remaining = (datetime.fromisoformat(policy['retry_after']) - datetime.now()).total_seconds()
                if remaining > 0:
                    self._retry_at = time.monotonic() + remaining
            except (TypeError, ValueError):
                pass

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='mihuashi-sync', daemon=True)
            self._thread.start()

    def submit(self, kind):
        if kind not in ('login', 'sync'):
            raise ValueError('unsupported task kind')
        self.start()
        with self._lock:
            if kind == 'sync' and self._sync_paused:
                run_id = uuid.uuid4().hex
                status_store.create_run(run_id, kind)
                status_store.finish_run(run_id, 'failed', {}, self._sync_paused)
                return {'task_id': run_id, 'queued': False, 'coalesced': False,
                        'blocked': self._sync_paused}
            if kind == 'sync' and self._retry_at and time.monotonic() < self._retry_at:
                run_id = uuid.uuid4().hex
                status_store.create_run(run_id, kind)
                status_store.finish_run(run_id, 'failed', {}, 'retry_backoff')
                return {'task_id': run_id, 'queued': False, 'coalesced': False,
                        'blocked': 'retry_backoff'}
            if kind == 'sync':
                if self._active and self._active['kind'] == kind:
                    return {'task_id': self._active['run_id'], 'queued': False, 'coalesced': True}
                if kind in self._pending:
                    return {'task_id': self._pending[kind], 'queued': True, 'coalesced': True}
            elif self._active and self._active['kind'] == kind:
                return {'task_id': self._active['run_id'], 'queued': False, 'coalesced': True}
            elif kind in self._pending:
                return {'task_id': self._pending[kind], 'queued': True, 'coalesced': True}
            run_id = uuid.uuid4().hex
            status_store.create_run(run_id, kind)
            self._pending[kind] = run_id
            self._queue.put((run_id, kind))
            return {'task_id': run_id, 'queued': True, 'coalesced': False}

    def status(self):
        result = status_store.get_status()
        with self._lock:
            result.update({
                'worker_running': bool(self._thread and self._thread.is_alive()),
                'active_task_id': self._active['run_id'] if self._active else None,
                'active_task_kind': self._active['kind'] if self._active else None,
                'last_login_window': self._last_login_window,
                'sync_paused': bool(self._sync_paused),
                'pause_reason': self._sync_paused,
            })
            if self._sync_paused == 'login_required':
                result['message'] = '登录状态失效；自动同步已暂停，请重新连接账号'
            elif self._sync_paused == 'platform_blocked':
                result['message'] = '平台要求人工检查或限制访问；同步已暂停，请稍后确认'
        return result

    def shutdown(self, timeout=5):
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            if job:
                run_id, _kind = job
                status_store.finish_run(run_id, 'cancelled', {}, 'shutdown')
        with self._lock:
            if not (thread and thread.is_alive()):
                self._thread = None

    def _run(self):
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=5)
            except queue.Empty:
                self._maybe_schedule_auto_sync()
                continue
            if job is None:
                continue
            run_id, kind = job
            with self._lock:
                self._pending.pop(kind, None)
                self._active = {'run_id': run_id, 'kind': kind}
            try:
                status_store.start_run(run_id)
                lock_path = browser.project_root() / '.runtime' / 'mihuashi' / 'profile.lock'
                with ProfileLock(lock_path):
                    if kind == 'login':
                        browser.open_login_window(self._stop)
                        self._last_login_window = 'closed'
                        self._sync_paused = None
                        self._retry_attempt = 0
                        self._retry_at = None
                        status_store.set_pause_reason(None)
                        status_store.set_retry_state(0, None)
                        status_store.finish_run(run_id, 'completed', {
                            'message': '登录窗口已关闭；账号登录状态将在采集适配器实测后校验',
                        })
                    else:
                        self._sync(run_id)
            except collector.AdapterNotConfigured:
                status_store.finish_run(run_id, 'failed', {}, 'adapter_not_configured')
            except collector.LoginRequired:
                self._sync_paused = 'login_required'
                status_store.set_pause_reason(self._sync_paused)
                status_store.finish_run(run_id, 'failed', {}, 'login_required')
            except collector.PlatformAccessBlocked:
                self._sync_paused = 'platform_blocked'
                status_store.set_pause_reason(self._sync_paused)
                status_store.finish_run(run_id, 'failed', {}, 'platform_blocked')
            except collector.TransientCollectionError:
                self._schedule_retry()
                status_store.finish_run(run_id, 'failed', {}, 'transient_error')
            except RuntimeError as exc:
                code = 'profile_locked' if 'already locked' in str(exc) else 'integration_error'
                if kind == 'sync' and code != 'profile_locked':
                    self._schedule_retry()
                status_store.finish_run(run_id, 'failed', {}, code)
            except ImportError:
                status_store.finish_run(run_id, 'failed', {}, 'browser_runtime_missing')
            except Exception:
                if kind == 'sync':
                    self._schedule_retry()
                status_store.finish_run(run_id, 'failed', {}, 'integration_error')
            finally:
                with self._lock:
                    self._active = None

    def _schedule_retry(self):
        self._retry_attempt = min(self._retry_attempt + 1, 8)
        base = min(60 * (2 ** (self._retry_attempt - 1)), 3600)
        delay = base * random.uniform(0.8, 1.2)
        self._retry_at = time.monotonic() + delay
        retry_after = (datetime.now() + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
        status_store.set_retry_state(self._retry_attempt, retry_after)

    def _maybe_schedule_auto_sync(self):
        settings = status_store.get_settings()
        enabled = settings.get('auto_sync_enabled', '0') == '1'
        if not enabled or not collector.COLLECTOR_READY or self._sync_paused:
            self._next_auto_sync = None
            return
        now = time.monotonic()
        if self._retry_at is not None:
            if now < self._retry_at:
                return
            self.submit('sync')
            self._retry_at = None
            self._next_auto_sync = now + int(settings.get('interval_minutes', '20')) * 60
            return
        if self._next_auto_sync is None:
            self._next_auto_sync = now
            if settings.get('sync_on_startup', '1') != '1':
                self._next_auto_sync = now + int(settings.get('interval_minutes', '20')) * 60
        if now >= self._next_auto_sync:
            self.submit('sync')
            self._next_auto_sync = now + int(settings.get('interval_minutes', '20')) * 60

    def _sync(self, run_id):
        settings = status_store.get_settings()
        orders = collector.collect_orders(
            headless=settings.get('headless_for_sync', '1') == '1',
            max_pages=int(settings.get('max_pages_per_run', '20')),
            stop_event=self._stop,
        )
        summary = {'collected': len(orders), 'created': 0, 'updated': 0,
                   'unchanged': 0, 'conflicts': 0, 'skipped': 0, 'failed': 0}
        allow_create = settings.get('allow_automatic_create', '0') == '1'
        for item in orders:
            if self._stop.is_set():
                status_store.finish_run(run_id, 'cancelled', summary, 'shutdown')
                return
            try:
                result = upsert_order(item, allow_create=allow_create)
                key = {'conflict': 'conflicts'}.get(result['action'], result['action'])
                if key not in summary:
                    key = 'failed'
                summary[key] += 1
            except Exception:
                summary['failed'] += 1
        status_store.finish_run(run_id, 'completed', summary)
        self._retry_attempt = 0
        self._retry_at = None
        status_store.set_retry_state(0, None)


_manager = None
_manager_lock = threading.Lock()


def get_manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = SyncManager()
            atexit.register(_manager.shutdown)
        return _manager


def start_manager():
    manager = get_manager()
    manager.start()
    return manager


def stop_manager():
    if _manager is not None:
        _manager.shutdown()
