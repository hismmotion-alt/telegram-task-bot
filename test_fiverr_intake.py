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
        self._jobs = []

    def run_once(self, callback, when, data, name):
        job = SimpleNamespace(
            callback=callback,
            when=when,
            data=data,
            name=name,
            removed=False,
        )
        job.schedule_removal = lambda: setattr(job, "removed", True)
        self._jobs.append(job)

    def jobs(self):
        return self._jobs


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

    async def get_chat_member(self, chat_id, user_id):
        status = main.ChatMemberStatus.ADMINISTRATOR if user_id == 42 else main.ChatMemberStatus.MEMBER
        return SimpleNamespace(status=status)


class FakeApplication:
    def __init__(self, fail_topic=False):
        self.bot = FakeBot(fail_topic=fail_topic)
        self.job_queue = FakeJobQueue()


class FakeTelegramMessage:
    def __init__(self, chat_id, message_thread_id=None, text=""):
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=900 + len(self.replies))


class FakeCallbackQuery:
    def __init__(self, data, chat_id, message_thread_id=None):
        self.data = data
        self.message = FakeTelegramMessage(chat_id, message_thread_id=message_thread_id)
        self.answers = []
        self.markup_removed = False
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_removed = reply_markup is None

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


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

    async def test_fiverr_assignment_prompt_includes_recent_assignee_button(self):
        main.save_task(
            title="Recent task",
            assignee="@designer",
            deadline_utc=datetime.now(tz=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        inbound = email(
            "Great news: You've received an order from buttonbuyer",
            "You just received an order from buttonbuyer.\nOrder #FO1515151515 is due Oct 9, 2026.",
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")

        assignment_prompt = self.app.bot.messages[-1]
        keyboard = assignment_prompt["reply_markup"].inline_keyboard
        self.assertIn("Tap a name or reply with @username.", assignment_prompt["text"])
        self.assertEqual(keyboard[0][0].text, "@designer")
        self.assertRegex(keyboard[0][0].callback_data, r"^task_assign:\d+:designer$")

    async def test_new_fiverr_task_created_message_has_control_panel(self):
        inbound = email(
            "Great news: You've received an order from panelbuyer",
            "You just received an order from panelbuyer.\nOrder #FO1717171717 is due Oct 9, 2026.",
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")

        created_message = self.app.bot.messages[-2]
        keyboard = created_message["reply_markup"].inline_keyboard
        labels = [button.text for row in keyboard for button in row]
        urls = [button.url for row in keyboard for button in row if button.url]
        self.assertIn("Deadline", labels)
        self.assertIn("Complete", labels)
        self.assertIn("Open Fiverr", labels)
        self.assertEqual(urls, ["https://www.fiverr.com/users/funanimation1/manage_orders/FO1717171717"])

    async def test_assignment_button_sets_assignee_and_confirms(self):
        main.save_task(
            title="Recent task",
            assignee="@designer",
            deadline_utc=datetime.now(tz=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        inbound = email(
            "Great news: You've received an order from assignbutton",
            "You just received an order from assignbutton.\nOrder #FO1616161616 is due Oct 9, 2026.",
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")

        assignment_prompt = self.app.bot.messages[-1]
        callback_data = assignment_prompt["reply_markup"].inline_keyboard[0][0].callback_data
        query = FakeCallbackQuery(callback_data, -100123)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        await main.on_callback_query(update, context)

        with main.get_db() as conn:
            task = conn.execute(
                "SELECT * FROM tasks WHERE title = 'Fiverr order #FO1616161616'"
            ).fetchone()
            pending_count = conn.execute("SELECT COUNT(*) FROM pending_assignments").fetchone()[0]
        self.assertEqual(task["assignee"], "@designer")
        self.assertEqual(pending_count, 0)
        self.assertTrue(query.markup_removed)
        self.assertEqual(query.message.replies[0][0], "✅ Assigned to @designer.")
        self.assertIn("Assigned to @designer", query.answers[0][0])

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

    async def test_requirements_received_creates_from_waiting_requirements_due(self):
        waiting = email(
            "Get aligned before your project even begins",
            "Your order FO3232A942587 with Joseph A. due on Oct 9, 2026 is waiting for requirements.",
            html_body='<a href="https://fiverr.com/orders/FO3232A942587?email_name=work_plan_order_created">Create timeline</a>',
            message_id="waiting",
        )
        active = email(
            "Requirements are in. Now it's time to share a timeline.",
            "Joseph A. has sent the requirements and your order FO3232A942587 is now in progress.\n"
            "1. Could you please let me know which format you would prefer for the final animation?\n"
            ".riv .rev or rive link",
            html_body='<a href="https://fiverr.com/orders/FO3232A942587?email_name=work_plan_order_requirements_received">Review and create timeline</a>',
            message_id="active",
        )

        await main.process_inbound_email(self.app, waiting, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            pending = conn.execute("SELECT * FROM pending_fiverr_requirement_orders").fetchone()
        self.assertEqual(pending["order_id"], "FO3232A942587")
        self.assertEqual(pending["client_name"], "Joseph A")

        await main.process_inbound_email(self.app, active, "Gmail")
        with main.get_db() as conn:
            tasks = conn.execute("SELECT * FROM tasks").fetchall()
            orders = conn.execute("SELECT * FROM fiverr_orders").fetchall()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Fiverr order #FO3232A942587")
        self.assertEqual(orders[0]["order_id"], "FO3232A942587")
        self.assertEqual(orders[0]["client_name"], "Joseph A")
        self.assertEqual(len(self.app.bot.created_topics), 1)
        self.assertIn("FO3232A942587", self.app.bot.created_topics[0][1])

    async def test_requirements_received_without_due_evidence_is_quarantined(self):
        active = email(
            "Requirements are in. Now it's time to share a timeline.",
            "Standalone Buyer has sent the requirements and your order FO5656565656 is now in progress.",
            html_body='<a href="https://fiverr.com/orders/FO5656565656?email_name=work_plan_order_requirements_received">Review and create timeline</a>',
            message_id="active-no-due",
        )
        await main.process_inbound_email(self.app, active, "Gmail")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            row = conn.execute("SELECT * FROM inbound_quarantine").fetchone()
        self.assertEqual(row["order_id"], "FO5656565656")
        self.assertEqual(row["reason"], "missing_due_date")

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

    async def test_order_command_opens_existing_topic_panel(self):
        task_id = main.save_task(
            title="Fiverr order #FO1818181818",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=555,
        )
        message = FakeTelegramMessage(-100123, message_thread_id=555, text="/order")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
        )
        context = SimpleNamespace(bot=self.app.bot)

        await main.cmd_order(update, context)

        panel = self.app.bot.messages[-1]
        self.assertEqual(panel["message_thread_id"], 555)
        self.assertIn(f"Task #{task_id}", panel["text"])
        labels = [button.text for row in panel["reply_markup"].inline_keyboard for button in row]
        self.assertIn("Mark submitted", labels)

    async def test_order_submit_button_is_assignee_only_and_internal(self):
        task_id = main.save_task(
            title="Fiverr order #FO1919191919",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=556,
        )
        query = FakeCallbackQuery(f"order_ctl:{task_id}:submit", -100123, message_thread_id=556)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)

        await main.on_callback_query(update, context)

        with main.get_db() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
        self.assertEqual(status, "submitted")
        self.assertIn("Submitted internally", query.answers[0][0])
        self.assertIn("does not deliver anything to Fiverr", query.message.replies[0][0])

    async def test_order_submit_button_rejects_non_assignee(self):
        task_id = main.save_task(
            title="Fiverr order #FO2020202020",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=557,
        )
        query = FakeCallbackQuery(f"order_ctl:{task_id}:submit", -100123, message_thread_id=557)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=100, username="someoneelse"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)

        await main.on_callback_query(update, context)

        with main.get_db() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
        self.assertEqual(status, "assigned")
        self.assertEqual(query.answers[0][0], "🧑‍💻 Only the assignee can use this.")

    async def test_complete_button_requires_confirmation_and_creator(self):
        task_id = main.save_task(
            title="Fiverr order #FO2121212121",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=558,
        )
        task = main.get_task(task_id, -100123)
        await main.schedule_task_jobs(self.app, task)

        confirm_query = FakeCallbackQuery(f"order_ctl:{task_id}:confirm_done", -100123, message_thread_id=558)
        update = SimpleNamespace(
            callback_query=confirm_query,
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)
        await main.on_callback_query(update, context)
        self.assertIn("Confirm completion", confirm_query.answers[0][0])
        confirm_labels = [
            button.text
            for row in confirm_query.edits[0][1]["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("✅ Yes, complete", confirm_labels)

        done_query = FakeCallbackQuery(f"order_ctl:{task_id}:done", -100123, message_thread_id=558)
        update.callback_query = done_query
        await main.on_callback_query(update, context)

        with main.get_db() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
        self.assertEqual(status, "done")
        self.assertTrue(all(job.removed for job in self.app.job_queue.jobs()))
        self.assertEqual(done_query.message.replies[0][0], "✅ Task completed.\nGreat work 👏")

    async def test_order_button_rejects_cross_topic_click(self):
        task_id = main.save_task(
            title="Fiverr order #FO2222222223",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=559,
        )
        query = FakeCallbackQuery(f"order_ctl:{task_id}:start", -100123, message_thread_id=999)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)

        await main.on_callback_query(update, context)

        with main.get_db() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
        self.assertEqual(status, "assigned")
        self.assertEqual(query.answers[0][0], "⚠️ This button belongs to a different topic.")


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
