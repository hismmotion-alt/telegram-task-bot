import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock
import main


class OptionalDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old_path = main.DB_PATH
        main.DB_PATH = os.path.join(self.directory.name, 'tasks.db')

    def tearDown(self):
        main.DB_PATH = self.old_path
        self.directory.cleanup()

    def legacy(self):
        with main.get_db() as conn:
            conn.execute('''CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                assignee TEXT NOT NULL, deadline_utc TEXT NOT NULL,
                creator_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                message_thread_id INTEGER, status TEXT NOT NULL DEFAULT 'assigned',
                created_at_utc TEXT NOT NULL, completed_at_utc TEXT,
                extra TEXT DEFAULT 'keep')''')
            conn.execute("CREATE UNIQUE INDEX task_title_idx ON tasks(title, chat_id)")
            conn.execute("CREATE TABLE audit (task_id INTEGER)")
            conn.execute("CREATE VIEW task_view AS SELECT id, title FROM tasks")
            conn.execute("CREATE TABLE linked (task_id INTEGER REFERENCES tasks(id))")
            conn.execute("CREATE TRIGGER task_audit AFTER INSERT ON tasks BEGIN INSERT INTO audit VALUES (NEW.id); END")
            conn.execute("INSERT INTO tasks(id,title,assignee,deadline_utc,creator_id,chat_id,message_thread_id,status,created_at_utc,completed_at_utc,extra) VALUES(7,'legacy','@person','2026-12-01T12:00:00+00:00',42,123,99,'done','2026-11-01','2026-11-02','special')")
            conn.execute("UPDATE sqlite_sequence SET seq=500 WHERE name='tasks'")
            conn.execute("INSERT INTO linked VALUES(7)")

    def test_legacy_migration_preserves_data_indexes_trigger_sequence_and_is_repeatable(self):
        self.legacy()
        with main.get_db() as conn:
            before = tuple(conn.execute('SELECT * FROM tasks').fetchone())
        main.init_db()
        main.init_db()
        with main.get_db() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM tasks WHERE id=7').fetchone()), before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='task_title_idx'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT title FROM task_view WHERE id=7").fetchone()[0], "legacy")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)
            self.assertEqual(next(c for c in conn.execute('PRAGMA table_info(tasks)') if c['name']=='deadline_utc')['notnull'], 0)
        task_id = main.save_task('new', '@unassigned', None, 42, 123, 100)
        self.assertGreater(task_id, 500)
        with main.get_db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0], 2)
        self.assertIsNone(main.get_task(task_id, 123).deadline_utc)

    def test_empty_legacy_table_keeps_deleted_id_high_water(self):
        self.legacy()
        with main.get_db() as conn:
            conn.execute("DELETE FROM tasks")
            old = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='tasks'").fetchone()[0]
        main.init_db()
        task_id = main.save_task('new', '@unassigned', None, 42, 123, 100)
        self.assertGreater(task_id, old)

    def test_null_deadline_roundtrip_formatting_reports_and_ordering(self):
        main.init_db()
        null_id = main.save_task('undated', '@unassigned', None, 42, 123, 99)
        dated_id = main.save_task('dated', '@person', datetime(2030,1,1,tzinfo=ZoneInfo('UTC')), 42,123,99)
        task = main.get_task(null_id,123)
        self.assertIsNone(task.deadline_utc)
        self.assertEqual(main.format_deadline_local(None), 'Not set')
        self.assertEqual(main.deadline_detail(None), 'Not set')
        self.assertIn('Not set', main.task_control_panel_text(task))
        self.assertIn('Not set', main.build_daily_update_message([task]))
        self.assertEqual([t.id for t in main.list_open_tasks(123)], [dated_id,null_id])

    async def test_null_deadline_does_not_schedule_or_send_stale_reminders(self):
        main.init_db()
        task_id = main.save_task('undated', '@unassigned', None,42,123,99)
        task = main.get_task(task_id,123)
        removed = []
        job = SimpleNamespace(name=f'task:{task_id}:3600', schedule_removal=lambda: removed.append(True))
        queue = SimpleNamespace(jobs=lambda: [job], run_once=lambda *a,**kw: self.fail('null task scheduled'))
        await main.schedule_task_jobs(SimpleNamespace(job_queue=queue), task)
        self.assertEqual(removed,[True])
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(bot=bot,job=SimpleNamespace(data={'task_id':task_id,'chat_id':123,'thread_id':99,'label':'(1h)'}))
        await main.remind_task_job(context)
        bot.send_message.assert_not_awaited()

    def test_migration_rollback_keeps_original_on_failure(self):
        self.legacy()
        with main.get_db() as conn:
            conn.execute('CREATE TABLE tasks_optional_deadline_migration (id INTEGER)')
        with self.assertRaises(sqlite3.OperationalError):
            main.init_db()
        with main.get_db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)
            self.assertEqual(next(c for c in conn.execute('PRAGMA table_info(tasks)') if c['name']=='deadline_utc')['notnull'],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='task_title_idx'").fetchone()[0],1)
