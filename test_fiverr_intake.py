import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from telegram.error import BadRequest

os.environ.setdefault("BOT_TOKEN", "test-token")

import main


class FakeJobQueue:
    def __init__(self):
        self._jobs = []
        self.daily_jobs = []
        self.repeating_jobs = []

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

    def run_daily(self, callback, time, name):
        job = SimpleNamespace(callback=callback, time=time, name=name)
        self.daily_jobs.append(job)
        self._jobs.append(job)

    def run_repeating(self, callback, interval, first, name):
        job = SimpleNamespace(callback=callback, interval=interval, first=first, name=name)
        self.repeating_jobs.append(job)
        self._jobs.append(job)

    def jobs(self):
        return self._jobs


class FakeBot:
    def __init__(self, fail_topic=False, send_exceptions=None):
        self.created_topics = []
        self.messages = []
        self.next_message_id = 100
        self.fail_topic = fail_topic
        self.send_exceptions = list(send_exceptions or [])

    async def create_forum_topic(self, chat_id, name):
        if self.fail_topic:
            raise RuntimeError("synthetic topic failure")
        thread_id = 1000 + len(self.created_topics)
        self.created_topics.append((chat_id, name, thread_id))
        return SimpleNamespace(message_thread_id=thread_id)

    async def send_message(self, **kwargs):
        if self.send_exceptions:
            exc = self.send_exceptions.pop(0)
            if exc:
                raise exc
        self.next_message_id += 1
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=self.next_message_id)

    async def get_chat_member(self, chat_id, user_id):
        status = main.ChatMemberStatus.ADMINISTRATOR if user_id == 42 else main.ChatMemberStatus.MEMBER
        return SimpleNamespace(status=status)


class FakeApplication:
    def __init__(self, fail_topic=False, send_exceptions=None):
        self.bot = FakeBot(fail_topic=fail_topic, send_exceptions=send_exceptions)
        self.job_queue = FakeJobQueue()


class FakeTelegramMessage:
    def __init__(self, chat_id, message_thread_id=None, text="", reply_to_message=None):
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id
        self.text = text
        self.reply_to_message = reply_to_message
        self.replies = []
        self.message_id = 123

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=900 + len(self.replies))


class FakeCallbackQuery:
    def __init__(self, data, chat_id, message_thread_id=None):
        self.data = data
        self.message = FakeTelegramMessage(chat_id, message_thread_id=message_thread_id)
        self.message.message_id = 777
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

    async def test_new_fiverr_task_created_message_enables_bottom_keyboard(self):
        inbound = email(
            "Great news: You've received an order from keyboardbuyer",
            "You just received an order from keyboardbuyer.\nOrder #FO1717171718 is due Oct 9, 2026.",
        )
        await main.process_inbound_email(self.app, inbound, "Gmail")

        created_message = self.app.bot.messages[-3]
        keyboard = created_message["reply_markup"]
        labels = [button.text for row in keyboard.keyboard for button in row]
        self.assertEqual(labels, [main.ORDER_CONTROLS_LABEL, main.DEADLINE_LABEL])
        self.assertTrue(keyboard.resize_keyboard)
        self.assertFalse(keyboard.one_time_keyboard)
        self.assertTrue(keyboard.is_persistent)

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

        panel = self.app.bot.messages[-2]
        self.assertEqual(panel["message_thread_id"], 555)
        self.assertIn(f"Task #{task_id}", panel["text"])
        labels = [button.text for row in panel["reply_markup"].inline_keyboard for button in row]
        self.assertIn("Mark submitted", labels)

    async def test_order_command_enables_bottom_keyboard(self):
        task_id = main.save_task(
            title="Fiverr order #FO1818181819",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=560,
        )
        message = FakeTelegramMessage(-100123, message_thread_id=560, text="/order")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
        )
        context = SimpleNamespace(bot=self.app.bot)

        await main.cmd_order(update, context)

        setup = self.app.bot.messages[-1]
        self.assertEqual(setup["message_thread_id"], 560)
        self.assertIn("Keyboard shortcuts enabled", setup["text"])
        labels = [button.text for row in setup["reply_markup"].keyboard for button in row]
        self.assertEqual(labels, [main.ORDER_CONTROLS_LABEL, main.DEADLINE_LABEL])
        self.assertIn(f"Task #{task_id}", self.app.bot.messages[-2]["text"])

    async def test_menu_command_enables_bottom_keyboard_without_task_lookup(self):
        message = FakeTelegramMessage(-100123, message_thread_id=561, text="/menu")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
        )
        context = SimpleNamespace(bot=self.app.bot)

        await main.cmd_menu(update, context)

        setup = self.app.bot.messages[-1]
        self.assertEqual(setup["message_thread_id"], 561)
        labels = [button.text for row in setup["reply_markup"].keyboard for button in row]
        self.assertEqual(labels, [main.ORDER_CONTROLS_LABEL, main.DEADLINE_LABEL])

    async def test_order_controls_shortcut_resolves_current_topic(self):
        topic_a = main.save_task(
            title="Fiverr order #FO2323232323",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=562,
        )
        topic_b = main.save_task(
            title="Fiverr order #FO2424242424",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 10, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=563,
        )
        message = FakeTelegramMessage(-100123, message_thread_id=563, text=main.ORDER_CONTROLS_LABEL)
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        handled = await main.handle_order_shortcut_message(update, context)

        self.assertTrue(handled)
        panel = self.app.bot.messages[-1]
        self.assertNotIn(f"Task #{topic_a}", panel["text"])
        self.assertIn(f"Task #{topic_b}", panel["text"])
        self.assertEqual(panel["message_thread_id"], 563)

    async def test_deadline_shortcut_is_read_only_and_topic_scoped(self):
        task_id = main.save_task(
            title="Fiverr order #FO2525252525",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=564,
        )
        message = FakeTelegramMessage(-100123, message_thread_id=564, text=main.DEADLINE_LABEL)
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        handled = await main.handle_order_shortcut_message(update, context)

        with main.get_db() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
        self.assertTrue(handled)
        self.assertEqual(status, "assigned")
        self.assertIn(f"Task #{task_id} deadline", message.replies[0][0])
        labels = [button.text for row in message.replies[0][1]["reply_markup"].keyboard for button in row]
        self.assertEqual(labels, [main.ORDER_CONTROLS_LABEL, main.DEADLINE_LABEL])

    async def test_shortcut_missing_topic_task_does_not_use_global_last_task(self):
        main.save_task(
            title="Fiverr order #FO2626262626",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=565,
        )
        message = FakeTelegramMessage(-100123, message_thread_id=999, text=main.ORDER_CONTROLS_LABEL)
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        handled = await main.handle_order_shortcut_message(update, context)

        self.assertTrue(handled)
        self.assertEqual(self.app.bot.messages, [])
        self.assertIn("No open order task in this topic", message.replies[0][0])

    async def test_shortcut_multiple_tasks_shows_selector(self):
        task_a = main.save_task(
            title="Fiverr order #FO2727272727",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=566,
        )
        task_b = main.save_task(
            title="Fiverr order #FO2828282828",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 10, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=566,
        )
        message = FakeTelegramMessage(-100123, message_thread_id=566, text=main.ORDER_CONTROLS_LABEL)
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        handled = await main.handle_order_shortcut_message(update, context)

        self.assertTrue(handled)
        self.assertEqual(self.app.bot.messages, [])
        self.assertIn(f"/order <id>", message.replies[0][0])
        self.assertIn(f"#{task_a}", message.replies[0][0])
        self.assertIn(f"#{task_b}", message.replies[0][0])

    async def test_shortcut_labels_do_not_disrupt_assignment_username_reply(self):
        task_id = main.save_task(
            title="Fiverr order #FO2929292929",
            assignee="@unassigned",
            deadline_utc=datetime(2026, 10, 9, 15, 52, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=567,
        )
        main.create_pending_assignment(
            chat_id=-100123,
            task_id=task_id,
            thread_id=567,
            owner_id=42,
            prompt_message_id=321,
        )
        prompt = SimpleNamespace(message_id=321)
        message = FakeTelegramMessage(-100123, message_thread_id=567, text="@designer", reply_to_message=prompt)
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        await main.on_reply_message(update, context)

        with main.get_db() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(task["assignee"], "@designer")
        self.assertEqual(message.replies[0][0], "✅ Assigned to @designer.")

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

    async def test_completion_migration_preserves_legacy_done_and_tracks_new_done(self):
        with main.get_db() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                (title, assignee, deadline_utc, creator_id, chat_id, message_thread_id, status, created_at_utc)
                VALUES ('Legacy done', '@old', ?, 42, -100123, NULL, 'done', ?)
                """,
                (
                    datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")).isoformat(),
                    datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")).isoformat(),
                ),
            )
        main.init_db()
        with main.get_db() as conn:
            legacy = conn.execute("SELECT completed_at_utc FROM tasks WHERE title = 'Legacy done'").fetchone()
            tracking = conn.execute("SELECT value FROM settings WHERE key = 'tracking_start_utc'").fetchone()
        self.assertIsNone(legacy["completed_at_utc"])
        self.assertIsNotNone(tracking["value"])

        task_id = main.save_task(
            title="Manual tracked task",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        main.mark_task_done(task_id, -100123)
        with main.get_db() as conn:
            row = conn.execute("SELECT status, completed_at_utc FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["completed_at_utc"])

    async def test_repeated_completion_does_not_move_completed_at_and_stale_start_cannot_reopen(self):
        task_id = main.save_task(
            title="Fiverr order #FO3030303030",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=570,
        )
        main.mark_task_done(task_id, -100123)
        with main.get_db() as conn:
            first_completed_at = conn.execute("SELECT completed_at_utc FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        main.mark_task_done(task_id, -100123)
        self.assertFalse(main.set_task_status(task_id, -100123, "in_progress"))
        with main.get_db() as conn:
            row = conn.execute("SELECT status, completed_at_utc FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["completed_at_utc"], first_completed_at)

        query = FakeCallbackQuery(f"order_ctl:{task_id}:start", -100123, message_thread_id=570)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)
        await main.on_callback_query(update, context)

        with main.get_db() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        self.assertEqual(status, "done")
        self.assertEqual(query.answers[0][0], "This task is already completed.")

    async def test_legacy_ack_and_delivery_callbacks_cannot_reopen_done_task(self):
        task_id = main.save_task(
            title="Legacy callback done",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=571,
        )
        main.mark_task_done(task_id, -100123)

        ack_query = FakeCallbackQuery(f"task_ack:{task_id}:yes", -100123, message_thread_id=571)
        update = SimpleNamespace(
            callback_query=ack_query,
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)
        await main.on_callback_query(update, context)

        delivery_query = FakeCallbackQuery(f"task_delivery:{task_id}:sent", -100123, message_thread_id=571)
        update.callback_query = delivery_query
        update.effective_user = SimpleNamespace(id=42, username="owner")
        await main.on_callback_query(update, context)

        with main.get_db() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        self.assertEqual(status, "done")
        self.assertEqual(ack_query.answers[0][0], "This task is already completed.")
        self.assertEqual(delivery_query.answers[0][0], "This task is already completed.")

    async def test_completed_projects_report_counts_done_only_and_splits_fiverr(self):
        main.set_setting("tracking_start_utc", "2026-10-01T00:00:00+00:00")
        fiverr_task = main.save_task(
            title="Fiverr order #FO3131313131",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=572,
        )
        manual_task = main.save_task(
            title="Internal render cleanup",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        submitted_task = main.save_task(
            title="Submitted but not done",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        with main.get_db() as conn:
            conn.execute(
                """
                INSERT INTO fiverr_orders
                (order_id, chat_id, client_name, task_id, topic_thread_id, source, status, created_at_utc, updated_at_utc)
                VALUES ('FO3131313131', -100123, 'buyer', ?, 572, 'test', 'created', ?, ?)
                """,
                (fiverr_task, datetime.now(tz=ZoneInfo("UTC")).isoformat(), datetime.now(tz=ZoneInfo("UTC")).isoformat()),
            )
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at_utc = ? WHERE id IN (?, ?)",
                ("2026-10-07T18:00:00+00:00", fiverr_task, manual_task),
            )
            conn.execute("UPDATE tasks SET status = 'submitted' WHERE id = ?", (submitted_task,))

        _, text = main.build_completed_projects_report(
            "weekly",
            datetime(2026, 10, 12, 16, 0, tzinfo=ZoneInfo("UTC")),
        )
        self.assertIn("Tracked completions: 2", text)
        self.assertIn("Fiverr order projects: 1", text)
        self.assertIn("Other tracked tasks: 1", text)
        self.assertIn("FO3131313131", text)
        self.assertNotIn("Submitted but not done", text)

    async def test_report_skips_wholly_pretracking_and_marks_partial_coverage(self):
        main.set_setting("tracking_start_utc", "2026-10-09T16:32:00+00:00")
        task_id = main.save_task(
            title="Tracked after start",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 10, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        with main.get_db() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at_utc = ? WHERE id = ?",
                ("2026-10-10T18:00:00+00:00", task_id),
            )

        _, weekly = main.build_completed_projects_report(
            "weekly",
            datetime(2026, 10, 12, 16, 0, tzinfo=ZoneInfo("UTC")),
        )
        _, monthly = main.build_completed_projects_report(
            "monthly",
            datetime(2026, 10, 12, 16, 0, tzinfo=ZoneInfo("UTC")),
        )

        self.assertIsNone(monthly)
        self.assertIn("Tracked completions: 1", weekly)
        self.assertIn("earlier part of this period is unknown", weekly)
        self.assertNotIn("Done projects", weekly)

    async def test_report_after_tracking_start_with_no_completions_says_none_tracked(self):
        main.set_setting("tracking_start_utc", "2026-10-01T00:00:00+00:00")

        _, text = main.build_completed_projects_report(
            "weekly",
            datetime(2026, 10, 19, 16, 0, tzinfo=ZoneInfo("UTC")),
        )

        self.assertIn("Tracked completions: 0", text)
        self.assertIn("none tracked in the covered window", text)
        self.assertNotIn("none completed in this period", text)

    async def test_weekly_report_dedupe_prevents_restart_duplicate(self):
        main.set_setting("general_thread_id", "10")
        task_id = main.save_task(
            title="Weekly completed task",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        with main.get_db() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at_utc = ? WHERE id = ?",
                ("2026-10-07T18:00:00+00:00", task_id),
            )
        context = SimpleNamespace(bot=self.app.bot)
        now = datetime(2026, 10, 12, 16, 0, tzinfo=ZoneInfo("UTC"))

        await main.completed_projects_report_job(context, "weekly", now)
        await main.completed_projects_report_job(context, "weekly", now)

        self.assertEqual(len(self.app.bot.messages), 1)
        self.assertEqual(self.app.bot.messages[0]["message_thread_id"], 10)

    async def test_report_boundaries_handle_dst_and_year_change(self):
        start, end, label = main.previous_week_bounds_utc(
            datetime(2026, 11, 9, 17, 0, tzinfo=ZoneInfo("UTC"))
        )
        self.assertEqual(start.astimezone(main.TZ).strftime("%Y-%m-%d %H:%M %Z"), "2026-11-02 00:00 PST")
        self.assertEqual(end.astimezone(main.TZ).strftime("%Y-%m-%d %H:%M %Z"), "2026-11-09 00:00 PST")
        self.assertIn("Nov", label)

        start, end, label = main.previous_month_bounds_utc(
            datetime(2027, 1, 1, 18, 0, tzinfo=ZoneInfo("UTC"))
        )
        self.assertEqual(start.astimezone(main.TZ).strftime("%Y-%m-%d %H:%M"), "2026-12-01 00:00")
        self.assertEqual(end.astimezone(main.TZ).strftime("%Y-%m-%d %H:%M"), "2027-01-01 00:00")
        self.assertEqual(label, "December 2026")

    async def test_startup_schedules_use_la_timezone_and_defaults(self):
        async def noop_start_webserver(app):
            return None

        old_start_webserver = main.start_webserver
        main.start_webserver = noop_start_webserver
        try:
            await main.on_startup(self.app)
        finally:
            main.start_webserver = old_start_webserver
        daily = {job.name: job for job in self.app.job_queue.daily_jobs}
        self.assertEqual(daily["daily_update_10pm"].time.tzinfo, main.TZ)
        self.assertEqual(daily["daily_update_10pm"].time.hour, 22)
        self.assertEqual(daily["weekly_completed_projects"].time.hour, 9)
        self.assertEqual(daily["monthly_completed_projects"].time.hour, 9)
        self.assertEqual(daily["game_invites"].time.hour, 15)

    async def test_game_invite_skips_without_configured_games_topic(self):
        context = SimpleNamespace(bot=self.app.bot)
        await main.game_invite_job(context)
        self.assertEqual(self.app.bot.messages, [])

    async def test_game_callback_is_topic_scoped_and_purges_participants_on_close(self):
        session = main.create_game_session(
            -100123,
            600,
            datetime(2026, 10, 9, 22, 0, tzinfo=ZoneInfo("UTC")),
        )
        main.update_game_session_message(session["id"], 777)

        wrong_topic = FakeCallbackQuery(f"game:answer:{session['id']}:{session['answer']}", -100123, message_thread_id=601)
        update = SimpleNamespace(
            callback_query=wrong_topic,
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot)
        await main.on_callback_query(update, context)
        self.assertEqual(wrong_topic.answers[0][0], "This game belongs in the Games topic.")

        right_topic = FakeCallbackQuery(f"game:answer:{session['id']}:{session['answer']}", -100123, message_thread_id=600)
        update.callback_query = right_topic
        await main.on_callback_query(update, context)
        self.assertEqual(right_topic.answers[0][0], "Correct! 🎉")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM game_answers").fetchone()[0], 1)

        summary = main.close_game_session(session["id"])
        self.assertEqual(summary, "1/1 correct")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM game_answers").fetchone()[0], 0)
            stored = conn.execute("SELECT result_summary FROM game_sessions WHERE id = ?", (session["id"],)).fetchone()
        self.assertEqual(stored["result_summary"], "1/1 correct")

    async def test_games_topic_controls_do_not_create_live_topic(self):
        message = FakeTelegramMessage(-100123, message_thread_id=602, text="/games settopic")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        await main.cmd_games(update, context)

        self.assertEqual(self.app.bot.created_topics, [])
        self.assertIn("Games topic saved", message.replies[0][0])
        self.assertEqual(main.get_setting("games_thread_id"), "602")

    async def test_repeated_task_done_command_is_quiet_and_preserves_timestamp(self):
        task_id = main.save_task(
            title="Repeat done command",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        message = FakeTelegramMessage(-100123, text=f"/task done {task_id}")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)

        await main.cmd_task(update, context)
        with main.get_db() as conn:
            first_completed_at = conn.execute("SELECT completed_at_utc FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        await main.cmd_task(update, context)
        with main.get_db() as conn:
            second_completed_at = conn.execute("SELECT completed_at_utc FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]

        self.assertEqual(first_completed_at, second_completed_at)
        self.assertEqual(message.replies[0][0], "✅ Task completed.\nGreat work 👏")
        self.assertEqual(message.replies[1][0], "✅ This task is already completed.")

    async def test_scheduled_send_rejection_can_retry_but_timeout_stays_uncertain(self):
        reject_app = FakeApplication(send_exceptions=[BadRequest("chat not found")])
        sent = await main.send_scheduled_message(reject_app.bot, "report:reject", -100123, "hello")
        self.assertFalse(sent)
        with main.get_db() as conn:
            row = conn.execute("SELECT state, attempts FROM scheduled_sends WHERE key = 'report:reject'").fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], 1)

        sent = await main.send_scheduled_message(self.app.bot, "report:reject", -100123, "hello again")
        self.assertTrue(sent)
        with main.get_db() as conn:
            row = conn.execute("SELECT state, attempts, message_id FROM scheduled_sends WHERE key = 'report:reject'").fetchone()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["attempts"], 2)
        self.assertIsNotNone(row["message_id"])

        timeout_app = FakeApplication(send_exceptions=[TimeoutError("unknown delivery")])
        sent = await main.send_scheduled_message(timeout_app.bot, "report:timeout", -100123, "maybe")
        self.assertFalse(sent)
        sent = await main.send_scheduled_message(self.app.bot, "report:timeout", -100123, "do not retry")
        self.assertFalse(sent)
        self.assertEqual(len(self.app.bot.messages), 1)
        with main.get_db() as conn:
            row = conn.execute("SELECT state, attempts FROM scheduled_sends WHERE key = 'report:timeout'").fetchone()
        self.assertEqual(row["state"], "uncertain")
        self.assertEqual(row["attempts"], 1)

    async def test_scheduled_send_pending_blocks_concurrent_attempt(self):
        self.assertTrue(main.begin_scheduled_send("report:pending"))
        self.assertFalse(main.begin_scheduled_send("report:pending"))
        with main.get_db() as conn:
            row = conn.execute("SELECT state FROM scheduled_sends WHERE key = 'report:pending'").fetchone()
        self.assertEqual(row["state"], "pending")

    async def test_stale_pending_scheduled_send_becomes_uncertain_and_needs_resolution(self):
        old_now = datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("UTC"))
        self.assertTrue(main.begin_scheduled_send("report:stale", chat_id=-100123, thread_id=12, now=old_now))

        sent = await main.send_scheduled_message(
            self.app.bot,
            "report:stale",
            -100123,
            "do not send while uncertain",
            thread_id=12,
            now=old_now + timedelta(minutes=20),
        )

        self.assertFalse(sent)
        self.assertEqual(self.app.bot.messages, [])
        with main.get_db() as conn:
            row = conn.execute("SELECT state, error, attempts FROM scheduled_sends WHERE key = 'report:stale'").fetchone()
        self.assertEqual(row["state"], "uncertain")
        self.assertEqual(row["error"], "stale_pending_after_restart")
        self.assertEqual(row["attempts"], 1)

        self.assertFalse(main.resolve_scheduled_send("report:stale", -100999, "notdelivered"))
        self.assertTrue(main.resolve_scheduled_send("report:stale", -100123, "notdelivered"))
        sent = await main.send_scheduled_message(
            self.app.bot,
            "report:stale",
            -100123,
            "safe retry after admin says missing",
            thread_id=12,
        )

        self.assertTrue(sent)
        with main.get_db() as conn:
            row = conn.execute("SELECT state, attempts FROM scheduled_sends WHERE key = 'report:stale'").fetchone()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["attempts"], 2)

    async def test_admin_can_mark_uncertain_scheduled_send_delivered_with_message_id(self):
        timeout_app = FakeApplication(send_exceptions=[TimeoutError("unknown delivery")])
        sent = await main.send_scheduled_message(timeout_app.bot, "report:delivered", -100123, "maybe delivered")
        self.assertFalse(sent)

        self.assertTrue(main.resolve_scheduled_send("report:delivered", -100123, "delivered", 7777))

        with main.get_db() as conn:
            row = conn.execute(
                "SELECT state, message_id, error FROM scheduled_sends WHERE key = 'report:delivered'"
            ).fetchone()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["message_id"], 7777)
        self.assertIsNone(row["error"])

    async def test_due_report_catchup_after_startup_dedupes(self):
        main.set_setting("tracking_start_utc", "2026-09-01T00:00:00+00:00")
        main.set_setting("general_thread_id", "11")
        task_id = main.save_task(
            title="Catchup completed task",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=None,
        )
        with main.get_db() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at_utc = ? WHERE id = ?",
                ("2026-10-07T18:00:00+00:00", task_id),
            )
        context = SimpleNamespace(bot=self.app.bot)
        now = datetime(2026, 10, 12, 17, 0, tzinfo=ZoneInfo("UTC"))

        await main.catch_up_due_reports(context, now)
        await main.catch_up_due_reports(context, now)

        self.assertEqual(len(self.app.bot.messages), 2)
        texts = [message["text"] for message in self.app.bot.messages]
        self.assertEqual(sum("Weekly completed projects" in text for text in texts), 1)
        self.assertEqual(sum("Monthly completed projects" in text for text in texts), 1)

    async def test_game_rejects_forged_choice_without_persisting(self):
        session = main.create_game_session(
            -100123,
            603,
            datetime(2026, 10, 9, 22, 0, tzinfo=ZoneInfo("UTC")),
        )
        main.update_game_session_message(session["id"], 777)
        query = FakeCallbackQuery(f"game:answer:{session['id']}:Forged", -100123, message_thread_id=603)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        context = SimpleNamespace(bot=self.app.bot)

        await main.on_callback_query(update, context)

        self.assertEqual(query.answers[0][0], "That answer is not valid for this game.")
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM game_answers").fetchone()[0], 0)

    async def test_feedback_button_prompts_and_reply_saves_escaped_topic_message(self):
        task_id = main.save_task(
            title="Fiverr order #FO4141414141",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=604,
        )
        query = FakeCallbackQuery(f"order_ctl:{task_id}:feedback", -100123, message_thread_id=604)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)

        await main.on_callback_query(update, context)

        prompt = query.message.replies[0]
        self.assertIn("Send the feedback", prompt[0])
        prompt_message = SimpleNamespace(message_id=prompt[1]["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1])
        with main.get_db() as conn:
            pending = conn.execute("SELECT * FROM pending_feedback").fetchone()
        reply_to = SimpleNamespace(message_id=pending["prompt_message_id"])
        message = FakeTelegramMessage(
            -100123,
            message_thread_id=604,
            text="<b>Needs punchier timing</b>\nSecond line",
            reply_to_message=reply_to,
        )
        reply_update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )

        await main.on_reply_message(reply_update, context)

        display = self.app.bot.messages[-1]
        self.assertEqual(display["message_thread_id"], 604)
        self.assertEqual(display["parse_mode"], main.ParseMode.HTML)
        self.assertIn("&lt;b&gt;Needs punchier timing&lt;/b&gt;", display["text"])
        with main.get_db() as conn:
            saved = conn.execute("SELECT * FROM feedback_entries").fetchone()
            pending = conn.execute("SELECT status FROM pending_feedback").fetchone()
        self.assertEqual(saved["feedback_text"], "<b>Needs punchier timing</b>\nSecond line")
        self.assertEqual(pending["status"], "submitted")

    async def test_feedback_wrong_user_topic_cancel_and_expiry_are_ignored(self):
        task_id = main.save_task(
            title="Fiverr order #FO4242424242",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=605,
        )
        query = FakeCallbackQuery(f"order_ctl:{task_id}:feedback", -100123, message_thread_id=605)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)
        await main.on_callback_query(update, context)
        with main.get_db() as conn:
            pending = conn.execute("SELECT * FROM pending_feedback").fetchone()

        wrong_user_msg = FakeTelegramMessage(
            -100123,
            message_thread_id=605,
            text="wrong user",
            reply_to_message=SimpleNamespace(message_id=pending["prompt_message_id"]),
        )
        wrong_user_update = SimpleNamespace(
            message=wrong_user_msg,
            effective_message=wrong_user_msg,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        await main.on_reply_message(wrong_user_update, context)
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_entries").fetchone()[0], 0)

        wrong_topic_msg = FakeTelegramMessage(
            -100123,
            message_thread_id=606,
            text="wrong topic",
            reply_to_message=SimpleNamespace(message_id=pending["prompt_message_id"]),
        )
        wrong_topic_update = SimpleNamespace(
            message=wrong_topic_msg,
            effective_message=wrong_topic_msg,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        await main.on_reply_message(wrong_topic_update, context)
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_entries").fetchone()[0], 0)

        cancel_query = FakeCallbackQuery(f"feedback_cancel:{pending['request_id']}", -100123, message_thread_id=605)
        cancel_update = SimpleNamespace(
            callback_query=cancel_query,
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        await main.on_callback_query(cancel_update, context)
        self.assertEqual(cancel_query.answers[0][0], "Cancelled")

        late_msg = FakeTelegramMessage(
            -100123,
            message_thread_id=605,
            text="late",
            reply_to_message=SimpleNamespace(message_id=pending["prompt_message_id"]),
        )
        late_update = SimpleNamespace(
            message=late_msg,
            effective_message=late_msg,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        await main.on_reply_message(late_update, context)
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_entries").fetchone()[0], 0)

    async def test_feedback_expired_prompt_does_not_save(self):
        task_id = main.save_task(
            title="Fiverr order #FO4343434343",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=607,
        )
        query = FakeCallbackQuery(f"order_ctl:{task_id}:feedback", -100123, message_thread_id=607)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot, application=self.app)
        await main.on_callback_query(update, context)
        with main.get_db() as conn:
            pending = conn.execute("SELECT * FROM pending_feedback").fetchone()
            conn.execute(
                "UPDATE pending_feedback SET expires_at_utc = ? WHERE request_id = ?",
                ("2026-01-01T00:00:00+00:00", pending["request_id"]),
            )

        message = FakeTelegramMessage(
            -100123,
            message_thread_id=607,
            text="expired feedback",
            reply_to_message=SimpleNamespace(message_id=pending["prompt_message_id"]),
        )
        reply_update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )

        await main.on_reply_message(reply_update, context)

        self.assertIn("expired", message.replies[0][0])
        with main.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_entries").fetchone()[0], 0)
            status = conn.execute("SELECT status FROM pending_feedback WHERE request_id = ?", (pending["request_id"],)).fetchone()[0]
        self.assertEqual(status, "expired")

    async def test_feedback_partial_display_failure_saves_entry_and_explicit_redisplay(self):
        feedback_app = FakeApplication(send_exceptions=[None, TimeoutError("second chunk unknown")])
        task_id = main.save_task(
            title="Fiverr order #FO4444444444",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=608,
        )
        query = FakeCallbackQuery(f"order_ctl:{task_id}:feedback", -100123, message_thread_id=608)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=feedback_app.bot, application=self.app)
        await main.on_callback_query(update, context)
        with main.get_db() as conn:
            pending = conn.execute("SELECT * FROM pending_feedback").fetchone()

        feedback_text = "A" * 2700 + "<b>tail</b>"
        message = FakeTelegramMessage(
            -100123,
            message_thread_id=608,
            text=feedback_text,
            reply_to_message=SimpleNamespace(message_id=pending["prompt_message_id"]),
        )
        reply_update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )

        await main.on_reply_message(reply_update, context)

        with main.get_db() as conn:
            entry = conn.execute("SELECT * FROM feedback_entries").fetchone()
            chunk_count = conn.execute("SELECT COUNT(*) FROM feedback_display_chunks").fetchone()[0]
            pending_status = conn.execute("SELECT status FROM pending_feedback").fetchone()[0]
        self.assertEqual(entry["feedback_text"], feedback_text)
        self.assertEqual(entry["display_state"], "display_uncertain")
        self.assertEqual(chunk_count, 1)
        self.assertEqual(pending_status, "display_uncertain")
        self.assertEqual(len(feedback_app.bot.messages), 1)

        redisplay_message = FakeTelegramMessage(-100123, message_thread_id=608, text=f"/feedback {entry['id']}")
        redisplay_update = SimpleNamespace(
            message=redisplay_message,
            effective_message=redisplay_message,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        redisplay_context = SimpleNamespace(bot=self.app.bot)

        await main.cmd_feedback(redisplay_update, redisplay_context)

        self.assertEqual(len(self.app.bot.messages), 2)
        self.assertEqual(self.app.bot.messages[0]["message_thread_id"], 608)
        self.assertIn("Feedback #", self.app.bot.messages[0]["text"])
        self.assertIn("&lt;b&gt;tail&lt;/b&gt;", self.app.bot.messages[1]["text"])
        with main.get_db() as conn:
            entry = conn.execute("SELECT display_state, display_message_id FROM feedback_entries").fetchone()
        self.assertEqual(entry["display_state"], "sent")
        self.assertIsNotNone(entry["display_message_id"])

    async def test_feedback_redisplay_is_authorized_and_topic_scoped(self):
        task_id = main.save_task(
            title="Fiverr order #FO4545454545",
            assignee="@designer",
            deadline_utc=datetime(2026, 10, 9, tzinfo=ZoneInfo("UTC")),
            creator_id=42,
            chat_id=-100123,
            thread_id=609,
        )
        feedback_id = main.save_feedback_entry(
            request_id="request-45",
            chat_id=-100123,
            thread_id=609,
            task_id=task_id,
            user_id=42,
            username="owner",
            feedback_text="Approved text",
        )

        wrong_topic = FakeTelegramMessage(-100123, message_thread_id=610, text=f"/feedback {feedback_id}")
        update = SimpleNamespace(
            message=wrong_topic,
            effective_message=wrong_topic,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=42, username="owner"),
        )
        context = SimpleNamespace(bot=self.app.bot)
        await main.cmd_feedback(update, context)
        self.assertIn("different topic", wrong_topic.replies[0][0])
        self.assertEqual(self.app.bot.messages, [])

        wrong_user = FakeTelegramMessage(-100123, message_thread_id=609, text=f"/feedback {feedback_id}")
        update = SimpleNamespace(
            message=wrong_user,
            effective_message=wrong_user,
            effective_chat=SimpleNamespace(id=-100123),
            effective_user=SimpleNamespace(id=99, username="designer"),
        )
        await main.cmd_feedback(update, context)
        self.assertIn("Only the task creator or an admin", wrong_user.replies[0][0])
        self.assertEqual(self.app.bot.messages, [])


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
