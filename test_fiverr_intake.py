import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ.setdefault("BOT_TOKEN", "test-token")

import main


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, data, name):
        self.jobs.append((callback, when, data, name))


class FakeBot:
    def __init__(self, fail_topic=False):
        self.created_topics = []
        self.messages = []
        self.next_message_id = 100
        self.fail_topic = fail_topic

    async def create_forum_topic(self, chat_id, name):
        if self.fail_topic:
            raise RuntimeError("synthetic topic failure")
        thread_id = 1000 + len(self.created_topics)
        self.created_topics.append((chat_id, name, thread_id))
        return SimpleNamespace(message_thread_id=thread_id)

    async def send_message(self, **kwargs):
        self.next_message_id += 1
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=self.next_message_id)


class FakeApplication:
    def __init__(self, fail_topic=False):
        self.bot = FakeBot(fail_topic=fail_topic)
        self.job_queue = FakeJobQueue()


def email(subject, body="", html_body="", message_id="msg-1"):
    return main.InboundEmail(
        subject=subject,
        body=body,
        html_body=html_body,
        sender="Fiverr <noreply@e.fiverr.com>",
        message_id=message_id,
        received_at="2026-10-01T17:00:00Z",
    )


class FiverrIntakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.old_db_path = main.DB_PATH
        main.DB_PATH = self.tmp.name
        main.init_db()
        main.set_setting("general_chat_id", "-100123")
        main.set_setting("owner_user_id", "42")
        main.set_setting("owner_username", "@owner")
        self.app = FakeApplication()

    def tearDown(self):
        main.DB_PATH = self.old_db_path
        os.unlink(self.tmp.name)

    async def test_seller_start_creates_topic_and_task(self):
        inbound = email(
            "Great news: You've received an order from flowpest",
            "You just received an order from flowpest.\nOrder #FO1234567890 is due Oct 9, 2026.",
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")
        self.assertEqual(len(self.app.bot.created_topics), 1)
        self.assertIn("flowpest", self.app.bot.created_topics[0][1])
        with main.get_db() as conn:
            orders = conn.execute("SELECT * FROM fiverr_orders").fetchall()
            tasks = conn.execute("SELECT * FROM tasks").fetchall()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "FO1234567890")
        self.assertEqual(len(tasks), 1)

    async def test_repeat_same_message_does_not_create_duplicate(self):
        inbound = email(
            "Great news: You've received an order from barryco624",
            "You've just received an order from barryco624!\nOrder #FO2222222222 is due Oct 10, 2026.",
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")
        await main.process_inbound_email(self.app, inbound, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fiverr_orders").fetchone()[0], 1)
        self.assertEqual(len(self.app.bot.created_topics), 1)

    async def test_same_buyer_separate_orders_create_distinct_tasks(self):
        body1 = "You just received an order from repeatbuyer.\nOrder #FO3333333333 is due Oct 10, 2026."
        body2 = "You just received an order from repeatbuyer.\nOrder #FO4444444444 is due Oct 12, 2026."
        await main.process_inbound_email(self.app, email("Great news: You've received an order from repeatbuyer", body1, message_id="m1"), "Gmail")
        await main.process_inbound_email(self.app, email("Great news: You've received an order from repeatbuyer", body2, message_id="m2"), "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fiverr_orders").fetchone()[0], 2)
        self.assertEqual(len(self.app.bot.created_topics), 2)
        self.assertIn("FO3333333333", self.app.bot.created_topics[0][1])
        self.assertIn("FO4444444444", self.app.bot.created_topics[1][1])

    async def test_same_order_different_messages_uses_order_identity(self):
        body = "You just received an order from identitybuyer.\nOrder #FO1212121212 is due Oct 10, 2026."
        await main.process_inbound_email(self.app, email("Great news: You've received an order from identitybuyer", body, message_id="first"), "Gmail")
        await main.process_inbound_email(self.app, email("Great news: You've received an order from identitybuyer", body, message_id="second"), "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            order = conn.execute("SELECT * FROM fiverr_orders WHERE order_id = 'FO1212121212'").fetchone()
        self.assertEqual(order["first_message_id"], "first")
        self.assertEqual(order["last_message_id"], "second")
        self.assertEqual(len(self.app.bot.created_topics), 1)

    async def test_waiting_for_requirements_followup_does_not_create_topic(self):
        inbound = email(
            "Get aligned before your project even begins",
            "Your order FO5555555555 with Joseph A. due on Oct 9, 2026 is waiting for requirements.",
            html_body='<a href="https://fiverr.com/orders/FO5555555555?email_name=work_plan_order_created">Create timeline</a>',
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fiverr_orders").fetchone()[0], 0)
        self.assertEqual(len(self.app.bot.created_topics), 0)

    async def test_buyer_receipt_and_delivery_are_ignored(self):
        buyer = email(
            "Fiverr / Shopping / Status of your order No. FO6666666666",
            "Your order no. FO6666666666 has been created.",
            html_body='<a href="https://fiverr.com/orders/FO6666666666?email_name=gig_order_created_to_buyer">View</a>',
        )
        delivery = email(
            "Your order was delivered",
            "Order #FO7777777777 delivered to buyer.",
            html_body='<a href="https://fiverr.com/orders/FO7777777777?email_name=gig_order_delivered_to_buyer">View</a>',
        )
        await main.process_inbound_email(self.app, buyer, "Gmail")
        await main.process_inbound_email(self.app, delivery, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbound_quarantine").fetchone()[0], 0)

    async def test_html_only_seller_start_is_supported(self):
        inbound = email(
            "Great news: You've received an order from htmlbuyer",
            html_body="""
              <p>You've just received an order from htmlbuyer!</p>
              <p>Order <a href="https://fiverr.com/users/funanimation1/manage_orders/FO8888888888?email_name=gig_order_started_seller">FO8888888888</a> is due Oct 11, 2026.</p>
            """,
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    async def test_missing_due_date_is_quarantined(self):
        inbound = email(
            "Great news: You've received an order from nodue",
            "You just received an order from nodue. Order #FO9999999999.",
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            row = conn.execute("SELECT * FROM inbound_quarantine").fetchone()
        self.assertEqual(row["reason"], "missing_due_date")

    async def test_cutover_does_not_replay_existing_processed_followups(self):
        inbound = email(
            "Reminder: complete requirements",
            "Reminder for order FO1111111111. View requirements.",
            html_body='<a href="https://fiverr.com/orders/FO1111111111?email_name=view_requirements_reminder">View requirements</a>',
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 0)

    async def test_cutover_timestamp_rejects_old_unprocessed_order_email(self):
        main.set_setting("fiverr_cutover_utc", "2026-10-01T18:00:00Z")
        inbound = email(
            "Great news: You've received an order from oldbuyer",
            "You just received an order from oldbuyer.\nOrder #FO1313131313 is due Oct 12, 2026.",
            message_id="old-order",
        )
        inbound.received_at = "2026-10-01T17:59:59Z"
        await main.process_inbound_email(self.app, inbound, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fiverr_orders").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbound_quarantine").fetchone()[0], 0)

    async def test_uncertain_topic_create_failure_is_quarantined_and_not_retried(self):
        app = FakeApplication(fail_topic=True)
        inbound = email(
            "Great news: You've received an order from retrybuyer",
            "You just received an order from retrybuyer.\nOrder #FO1414141414 is due Oct 14, 2026.",
            message_id="retry-1",
        )
        await main.process_inbound_email(app, inbound, "Gmail")
        self.assertEqual(len(app.bot.created_topics), 0)
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            order = conn.execute("SELECT * FROM fiverr_orders WHERE order_id = 'FO1414141414'").fetchone()
            first_quarantine = conn.execute("SELECT reason FROM inbound_quarantine ORDER BY id").fetchall()
        self.assertEqual(order["status"], "quarantined:topic_create_failed")
        self.assertEqual([row["reason"] for row in first_quarantine], ["topic_create_failed"])

        await main.process_inbound_email(self.app, inbound, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            reasons = conn.execute("SELECT reason FROM inbound_quarantine ORDER BY id").fetchall()
        self.assertEqual([row["reason"] for row in reasons], ["topic_create_failed", "uncertain_create_retry"])


class AppsScriptDraftTests(unittest.TestCase):
    def test_failed_webhook_does_not_mark_or_label_processed(self):
        script = Path("fiverr_gmail_forwarder.gs").read_text(encoding="utf-8")
        self.assertIn("let threadComplete = true;", script)
        self.assertIn("threadComplete = false;", script)
        self.assertIn("if (threadComplete)", script)
        self.assertLess(script.index("if (res.getResponseCode() === 200)"), script.index("props.setProperty(processedKey"))

    def test_processed_ids_are_preserved_and_checked_before_fetch(self):
        script = Path("fiverr_gmail_forwarder.gs").read_text(encoding="utf-8")
        self.assertIn('const processedKey = "processed_" + messageId;', script)
        self.assertLess(script.index("props.getProperty(processedKey)"), script.index("UrlFetchApp.fetch"))

    def test_query_quote_is_safe_for_apostrophe(self):
        script = Path("fiverr_gmail_forwarder.gs").read_text(encoding="utf-8")
        self.assertIn("`(\"Great news: You've received an order from\"", script)
        self.assertNotIn("You\\\\\\'ve", script)


if __name__ == "__main__":
    unittest.main()
