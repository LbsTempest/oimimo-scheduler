import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import db
import app as flask_app
from integrations.mihuashi.schemas import ExternalOrder
from integrations.mihuashi.sync_service import upsert_order
from integrations.mihuashi.web_security import is_local_request
from integrations.mihuashi.profile_lock import ProfileLock
from integrations.mihuashi.cli_inspect import safe_url
from integrations.mihuashi.sync_manager import get_manager
from integrations.mihuashi.status_store import get_recent_runs
from pydantic import ValidationError


class MihuashiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.runtime_root = Path(__file__).resolve().parents[1] / '.runtime'
        self.runtime_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(self.runtime_root))
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'orders.db')
        db.init_db()
        self.client = flask_app.app.test_client()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()
        try:
            self.runtime_root.rmdir()
        except OSError:
            pass

    @staticmethod
    def sample(**changes):
        data = {
            'external_order_id': 'sample-101',
            'project_name': '示例插画订单',
            'platform_url': 'https://www.mihuashi.com/orders/sample-101',
            'quoted_amount': '1200.00',
            'deadline': '2026-11-20',
            'platform_status': 'in_progress',
        }
        data.update(changes)
        return ExternalOrder.model_validate(data)

    def test_external_order_requires_stable_id_and_trusted_platform_url(self):
        with self.assertRaises(ValidationError):
            ExternalOrder(project_name='no id')
        with self.assertRaises(ValidationError):
            self.sample(platform_url='https://example.com/orders/1')
        with self.assertRaises(ValidationError):
            self.sample(quoted_amount='NaN')

    def test_upsert_is_idempotent_and_preserves_local_fields(self):
        first = upsert_order(self.sample(), allow_create=True)
        self.assertEqual(first['action'], 'created')
        second = upsert_order(self.sample(), allow_create=True)
        self.assertEqual(second['action'], 'unchanged')
        local = db.get_order(first['local_order_id'])
        self.assertEqual(local['page_deadline'], '2026-11-20')
        self.assertIsNone(local['scheduled_start'])
        self.assertIsNone(local['scheduled_end'])
        self.assertEqual(local['deposit'], 0)

        ok, error = db.update_order(local['id'], {
            'project_name': '本人改过的标题',
            'notes': '保留的本地笔记',
            'current_stage': 'sketch',
            'scheduled_start': '2026-11-01',
            'scheduled_end': '2026-11-10',
        })
        self.assertTrue(ok, error)
        updated = upsert_order(self.sample(
            project_name='平台新标题', deadline='2026-11-25', platform_status='delivered'),
            allow_create=True,
        )
        self.assertEqual(updated['action'], 'conflict')
        local = db.get_order(local['id'])
        self.assertEqual(local['project_name'], '本人改过的标题')
        self.assertEqual(local['page_deadline'], '2026-11-25')
        self.assertEqual(local['notes'], '保留的本地笔记')
        self.assertEqual(local['current_stage'], 'sketch')
        self.assertEqual(local['scheduled_start'], '2026-11-01')
        self.assertEqual(local['scheduled_end'], '2026-11-10')

    def test_deleted_local_order_is_not_recreated(self):
        created = upsert_order(self.sample(), allow_create=True)
        self.assertTrue(db.delete_order(created['local_order_id']))
        result = upsert_order(self.sample(project_name='平台改名'), allow_create=True)
        self.assertEqual(result['action'], 'conflict')
        self.assertIsNone(result['local_order_id'])
        conn = db.get_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM external_order_links').fetchone()[0], 1)
        finally:
            conn.close()

    def test_unapproved_new_orders_are_skipped(self):
        result = upsert_order(self.sample(), allow_create=False)
        self.assertEqual(result['action'], 'skipped')
        conn = db.get_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
        finally:
            conn.close()

    def test_dry_run_previews_without_writing_orders_or_links(self):
        preview = upsert_order(self.sample(), allow_create=True, dry_run=True)
        self.assertEqual(preview['action'], 'would_create')
        self.assertIn('page_deadline', preview['fields'])
        conn = db.get_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM external_order_links').fetchone()[0], 0)
        finally:
            conn.close()

        created = upsert_order(self.sample(), allow_create=True)
        before = db.get_order(created['local_order_id'])
        changed = self.sample(project_name='新标题', deadline='2026-11-25')
        preview = upsert_order(changed, allow_create=True, dry_run=True)
        self.assertEqual(preview['action'], 'updated')
        self.assertIn('project_name', preview['fields'])
        self.assertIn('page_deadline', preview['fields'])
        after = db.get_order(created['local_order_id'])
        self.assertEqual(after['project_name'], before['project_name'])
        self.assertEqual(after['page_deadline'], before['page_deadline'])

    def test_integration_tables_survive_repeated_database_initialization(self):
        db.init_db()
        conn = db.get_db()
        try:
            tables = {row['name'] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn('external_order_links', tables)
            self.assertIn('mihuashi_sync_runs', tables)
        finally:
            conn.close()

    def test_integration_write_routes_require_exact_local_origin(self):
        def request(host, origin=None, scheme='http'):
            headers = {'Origin': origin} if origin else {}
            return SimpleNamespace(host=host, scheme=scheme, headers=headers)

        self.assertTrue(is_local_request(request('127.0.0.1:1096', 'http://127.0.0.1:1096'), True))
        self.assertFalse(is_local_request(request('127.0.0.1:1096'), True))
        self.assertFalse(is_local_request(request('127.0.0.1:1096', 'http://127.0.0.1:1097'), True))
        self.assertFalse(is_local_request(request('127.0.0.1:1096', 'https://www.mihuashi.com'), True))
        self.assertFalse(is_local_request(request('attacker.example', 'http://attacker.example'), True))

    def test_profile_lock_excludes_second_browser_owner(self):
        lock_path = Path(self.temp.name) / 'profile.lock'
        first = ProfileLock(lock_path)
        second = ProfileLock(lock_path)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_inspect_url_hides_query_and_numeric_order_ids(self):
        safe = safe_url('https://www.mihuashi.com/orders/123456?token=secret#detail')
        self.assertEqual(safe, 'https://www.mihuashi.com/orders/:id')

    def test_settings_page_and_settings_api_are_integrated(self):
        for path in ('/', '/calendar', '/orders/new'):
            page = self.client.get(path, base_url='http://127.0.0.1:1096', follow_redirects=True)
            self.assertEqual(page.status_code, 200, path)
        page = self.client.get('/settings', base_url='http://127.0.0.1:1096')
        self.assertEqual(page.status_code, 200)
        self.assertIn('米画师订单同步'.encode('utf-8'), page.data)
        status = self.client.get(
            '/api/integrations/mihuashi/status', base_url='http://127.0.0.1:1096')
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.get_json()['collector_ready'])

        bad = self.client.put(
            '/api/integrations/mihuashi/settings',
            base_url='http://127.0.0.1:1096',
            headers={'Origin': 'http://127.0.0.1:1096'},
            json={'interval_minutes': 5},
        )
        self.assertEqual(bad.status_code, 400)
        premature = self.client.put(
            '/api/integrations/mihuashi/settings',
            base_url='http://127.0.0.1:1096',
            headers={'Origin': 'http://127.0.0.1:1096'},
            json={'auto_sync_enabled': True},
        )
        self.assertEqual(premature.status_code, 409)
        good = self.client.put(
            '/api/integrations/mihuashi/settings',
            base_url='http://127.0.0.1:1096',
            headers={'Origin': 'http://127.0.0.1:1096'},
            json={'interval_minutes': 30, 'auto_sync_enabled': False},
        )
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.get_json()['settings']['interval_minutes'], '30')

    def test_sync_api_runs_off_request_thread_and_fails_closed_without_adapter(self):
        manager = get_manager()
        try:
            response = self.client.post(
                '/api/integrations/mihuashi/sync',
                base_url='http://127.0.0.1:1096',
                headers={'Origin': 'http://127.0.0.1:1096'},
            )
            self.assertEqual(response.status_code, 202)
            task_id = response.get_json()['task_id']
            deadline = time.time() + 5
            result = None
            while time.time() < deadline:
                runs = get_recent_runs(1)
                result = runs[0] if runs else None
                if result and result['run_id'] == task_id and result['status'] != 'queued':
                    break
                time.sleep(0.05)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error_code'], 'adapter_not_configured')
            conn = db.get_db()
            try:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
            finally:
                conn.close()
        finally:
            manager.shutdown()
            lock_file = Path(__file__).resolve().parents[1] / '.runtime/mihuashi/profile.lock'
            try:
                lock_file.unlink()
                lock_file.parent.rmdir()
                lock_file.parent.parent.rmdir()
            except OSError:
                pass


if __name__ == '__main__':
    unittest.main()
