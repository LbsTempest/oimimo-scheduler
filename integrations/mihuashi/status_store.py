"""米画师集成设置和运行状态的 SQLite 访问层。"""

import json

import db


DEFAULT_SETTINGS = {
    'mihuashi_auto_sync_enabled': '0',
    'mihuashi_sync_on_startup': '1',
    'mihuashi_interval_minutes': '20',
    'mihuashi_max_pages_per_run': '20',
    'mihuashi_headless_for_sync': '1',
    'mihuashi_allow_automatic_create': '0',
}


def get_settings():
    all_settings = db.get_all_settings()
    return {key.removeprefix('mihuashi_'): all_settings.get(key, value)
            for key, value in DEFAULT_SETTINGS.items()}


def update_settings(values):
    db.update_settings({'mihuashi_' + key: str(value) for key, value in values.items()})


def get_policy_state():
    values = db.get_all_settings()
    return {
        'pause_reason': values.get('mihuashi_sync_paused', ''),
        'retry_attempt': values.get('mihuashi_retry_attempt', '0'),
        'retry_after': values.get('mihuashi_retry_after', ''),
    }


def set_pause_reason(reason):
    db.update_settings({'mihuashi_sync_paused': reason or ''})


def set_retry_state(attempt, retry_after):
    db.update_settings({
        'mihuashi_retry_attempt': str(attempt),
        'mihuashi_retry_after': retry_after or '',
    })


def create_run(run_id, kind):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mihuashi_sync_runs (run_id, kind, status) VALUES (?, ?, 'queued')",
            (run_id, kind),
        )


def start_run(run_id):
    with db.transaction() as conn:
        conn.execute(
            "UPDATE mihuashi_sync_runs SET status='running', started_at=datetime('now','localtime') "
            'WHERE run_id=?', (run_id,),
        )


def finish_run(run_id, status, summary=None, error_code=None):
    with db.transaction() as conn:
        conn.execute(
            "UPDATE mihuashi_sync_runs SET status=?, summary_json=?, error_code=?, "
            "completed_at=datetime('now','localtime'), duration_ms=CAST(("
            "julianday('now','localtime')-julianday(started_at))*86400000 AS INTEGER) "
            "WHERE run_id=?",
            (status, json.dumps(summary or {}, ensure_ascii=False, separators=(',', ':')),
             error_code, run_id),
        )


def get_recent_runs(limit=10):
    conn = db.get_db()
    try:
        rows = conn.execute(
            'SELECT run_id, kind, status, summary_json, error_code, created_at, started_at, completed_at, duration_ms '
            'FROM mihuashi_sync_runs ORDER BY id DESC LIMIT ?', (int(limit),)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item['summary'] = json.loads(item.pop('summary_json') or '{}')
            except (TypeError, ValueError):
                item['summary'] = {}
                item.pop('summary_json', None)
            result.append(item)
        return result
    finally:
        conn.close()


def get_status():
    runs = get_recent_runs(1)
    settings = get_settings()
    from . import collector
    ready = bool(collector.COLLECTOR_READY)
    return {
        'configured': ready,
        'login_status': 'unknown',
        'collector_ready': ready,
        'worker_running': False,
        'active_task_id': None,
        'settings': settings,
        'last_run': runs[0] if runs else None,
        'recent_runs': get_recent_runs(10),
        'message': '等待本人账号完成订单列表、详情和分页勘察' if not ready else '采集器已配置，登录状态待检查',
    }
