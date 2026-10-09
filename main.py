import hashlib
import hmac
import html
import logging
import os
import re
import shlex
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Iterator, Optional

from aiohttp import web
from dateutil import parser as date_parser
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
import random


load_dotenv()

DB_PATH = os.getenv("BOT_DB_PATH", "tasks.db")
TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/Los_Angeles"))
HTTP_PORT = int(os.getenv("PORT", "8000"))
MAILGUN_SIGNING_KEY = os.getenv("MAILGUN_SIGNING_KEY", "")
MAILGUN_ALLOWED_SENDER = os.getenv("MAILGUN_ALLOWED_SENDER", "fiverr.com")
GMAIL_WEBHOOK_TOKEN = os.getenv("GMAIL_WEBHOOK_TOKEN", "")
ORDER_CONTROLS_LABEL = "Order controls"
DEADLINE_LABEL = "Deadline"
REPORT_TIME_DEFAULT = "09:00"
GAME_INVITE_TIME_DEFAULT = "15:00"
GAME_CALLBACK_PREFIX = "game"


@dataclass
class Task:
    id: int
    title: str
    assignee: str
    deadline_utc: datetime
    creator_id: int
    chat_id: int
    thread_id: Optional[int]
    status: str


@dataclass
class InboundEmail:
    subject: str
    body: str
    html_body: str = ""
    sender: str = ""
    message_id: str = ""
    gmail_thread_id: str = ""
    received_at: str = ""


@dataclass
class FiverrOrder:
    order_id: str
    client_name: str
    due: datetime
    source: str
    message_id: str
    received_at: str = ""


@dataclass
class FiverrDecision:
    action: str
    reason: str
    order: Optional[FiverrOrder] = None


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                assignee TEXT NOT NULL,
                deadline_utc TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_thread_id INTEGER,
                status TEXT NOT NULL DEFAULT 'assigned',
                created_at_utc TEXT NOT NULL,
                completed_at_utc TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topics (
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                thread_id INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(chat_id, name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_assignments (
                chat_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                thread_id INTEGER,
                owner_id INTEGER NOT NULL,
                prompt_message_id INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY (chat_id, task_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fiverr_orders (
                order_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                task_id INTEGER,
                topic_thread_id INTEGER,
                source TEXT NOT NULL,
                first_message_id TEXT,
                last_message_id TEXT,
                status TEXT NOT NULL DEFAULT 'processing',
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_quarantine (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                message_id TEXT,
                order_id TEXT,
                reason TEXT NOT NULL,
                subject TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_fiverr_requirement_orders (
                order_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                due_utc TEXT NOT NULL,
                source TEXT NOT NULL,
                first_message_id TEXT,
                gmail_thread_id TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_sends (
                key TEXT PRIMARY KEY,
                chat_id INTEGER,
                thread_id INTEGER,
                state TEXT NOT NULL DEFAULT 'pending',
                claimed_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                sent_at_utc TEXT,
                message_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 1,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                feedback_text TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                display_message_id INTEGER,
                display_state TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_display_chunks (
                feedback_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at_utc TEXT NOT NULL,
                PRIMARY KEY (feedback_id, chunk_index)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_feedback (
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                prompt_message_id INTEGER NOT NULL,
                request_id TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (chat_id, thread_id, task_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_sessions (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                message_id INTEGER,
                prompt TEXT NOT NULL,
                answer TEXT NOT NULL,
                choices TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at_utc TEXT NOT NULL,
                closes_at_utc TEXT NOT NULL,
                result_summary TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_answers (
                session_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                choice TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                answered_at_utc TEXT NOT NULL,
                PRIMARY KEY (session_id, user_id)
            )
            """
        )
        # Add message_thread_id column for existing DBs
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "message_thread_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN message_thread_id INTEGER")
        if "completed_at_utc" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN completed_at_utc TEXT")
        scheduled_cols = [row["name"] for row in conn.execute("PRAGMA table_info(scheduled_sends)").fetchall()]
        if "chat_id" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN chat_id INTEGER")
        if "thread_id" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN thread_id INTEGER")
        if "state" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN state TEXT NOT NULL DEFAULT 'sent'")
        if "claimed_at_utc" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN claimed_at_utc TEXT NOT NULL DEFAULT ''")
        if "updated_at_utc" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN updated_at_utc TEXT NOT NULL DEFAULT ''")
        if "message_id" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN message_id INTEGER")
        if "attempts" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
        if "error" not in scheduled_cols:
            conn.execute("ALTER TABLE scheduled_sends ADD COLUMN error TEXT")
        feedback_cols = [row["name"] for row in conn.execute("PRAGMA table_info(feedback_entries)").fetchall()]
        if "request_id" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_entries ADD COLUMN request_id TEXT")
        if "display_message_id" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_entries ADD COLUMN display_message_id INTEGER")
        if "display_state" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_entries ADD COLUMN display_state TEXT NOT NULL DEFAULT 'sent'")
        game_cols = [row["name"] for row in conn.execute("PRAGMA table_info(game_sessions)").fetchall()]
        if "message_id" not in game_cols:
            conn.execute("ALTER TABLE game_sessions ADD COLUMN message_id INTEGER")
        if "choices" not in game_cols:
            conn.execute("ALTER TABLE game_sessions ADD COLUMN choices TEXT NOT NULL DEFAULT ''")
        if "result_summary" not in game_cols:
            conn.execute("ALTER TABLE game_sessions ADD COLUMN result_summary TEXT")
        if not conn.execute("SELECT value FROM settings WHERE key = 'tracking_start_utc'").fetchone():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('tracking_start_utc', ?)",
                (datetime.now(tz=ZoneInfo("UTC")).isoformat(),),
            )
        # Migrate legacy status values
        conn.execute("UPDATE tasks SET status = 'assigned' WHERE status = 'open'")


def parse_deadline(date_str: str, time_str: str) -> datetime:
    # Interpret as local timezone (PST/PDT from TZ setting)
    dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return dt_local.replace(tzinfo=TZ).astimezone(ZoneInfo("UTC"))


def format_deadline_local(deadline_utc: datetime) -> str:
    return deadline_utc.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")


def deadline_from_row(row: sqlite3.Row) -> datetime:
    return datetime.fromisoformat(row["deadline_utc"]).astimezone(ZoneInfo("UTC"))


def task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        assignee=row["assignee"],
        deadline_utc=deadline_from_row(row),
        creator_id=row["creator_id"],
        chat_id=row["chat_id"],
        thread_id=row["message_thread_id"],
        status=row["status"],
    )


def save_task(
    title: str,
    assignee: str,
    deadline_utc: datetime,
    creator_id: int,
    chat_id: int,
    thread_id: Optional[int],
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (title, assignee, deadline_utc, creator_id, chat_id, message_thread_id, status, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, 'assigned', ?)
            """,
            (
                title,
                assignee,
                deadline_utc.isoformat(),
                creator_id,
                chat_id,
                thread_id,
                datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            ),
        )
        return int(cur.lastrowid)


def mark_task_done(task_id: int, chat_id: int) -> tuple[Optional[Task], bool]:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND chat_id = ?",
            (task_id, chat_id),
        ).fetchone()
        if not row:
            return None, False
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = 'done',
                completed_at_utc = COALESCE(completed_at_utc, ?)
            WHERE id = ? AND chat_id = ? AND status != 'done'
            """,
            (now, task_id, chat_id),
        )
        transitioned = cur.rowcount == 1
        refreshed = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND chat_id = ?",
            (task_id, chat_id),
        ).fetchone()
        return (task_from_row(refreshed) if refreshed else None), transitioned

def mark_all_tasks_done(chat_id: int, thread_id: Optional[int]) -> int:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        if thread_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = 'done',
                    completed_at_utc = COALESCE(completed_at_utc, ?)
                WHERE chat_id = ? AND status != 'done' AND message_thread_id IS NULL
                """,
                (now, chat_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = 'done',
                    completed_at_utc = COALESCE(completed_at_utc, ?)
                WHERE chat_id = ? AND status != 'done' AND message_thread_id = ?
                """,
                (now, chat_id, thread_id),
            )
        return cur.rowcount


def mark_all_tasks_done_global(chat_id: int) -> int:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = 'done',
                completed_at_utc = COALESCE(completed_at_utc, ?)
            WHERE chat_id = ? AND status != 'done'
            """,
            (now, chat_id),
        )
        return cur.rowcount


def list_open_tasks(chat_id: int) -> list[Task]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status != 'done' ORDER BY deadline_utc ASC",
            (chat_id,),
        ).fetchall()
        return [task_from_row(r) for r in rows]


def list_open_tasks_by_thread(chat_id: int, thread_id: Optional[int]) -> list[Task]:
    with get_db() as conn:
        if thread_id is None:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND status != 'done' AND message_thread_id IS NULL ORDER BY deadline_utc ASC",
                (chat_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND status != 'done' AND message_thread_id = ? ORDER BY deadline_utc ASC",
                (chat_id, thread_id),
            ).fetchall()
        return [task_from_row(r) for r in rows]


def find_tasks_by_title(chat_id: int, query: str) -> list[Task]:
    pattern = f"%{query}%"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status != 'done' AND title LIKE ? ORDER BY deadline_utc ASC",
            (chat_id, pattern),
        ).fetchall()
        return [task_from_row(r) for r in rows]


def get_task(task_id: int, chat_id: int) -> Optional[Task]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND chat_id = ?",
            (task_id, chat_id),
        ).fetchone()
        return task_from_row(row) if row else None


def get_fiverr_order_by_task(task_id: int, chat_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM fiverr_orders
            WHERE task_id = ? AND chat_id = ? AND status = 'created'
            """,
            (task_id, chat_id),
        ).fetchone()


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or not update.effective_user:
        return False
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def task_reminder_text(task: Task, when: str) -> str:
    return (
        f"⏰ Reminder {when}:\n"
        f"Task #{task.id}: {task.title}\n"
        f"Assignee: {task.assignee}\n"
        f"Deadline: {format_deadline_local(task.deadline_utc)}"
    )


def status_emoji(status: str) -> str:
    return {
        "assigned": "🟡",
        "in_progress": "🔵",
        "submitted": "🟣",
        "revision": "🟠",
        "sent_to_client": "🟢",
        "done": "✅",
        "blocked": "🔴",
    }.get(status, "🟡")


def status_label(status: str) -> str:
    return {
        "assigned": "Assigned",
        "in_progress": "In Progress",
        "submitted": "Submitted",
        "revision": "Revision",
        "sent_to_client": "Sent to Client",
        "done": "Done",
        "blocked": "Blocked",
    }.get(status, "Assigned")


def custom_emoji_markup(name: str, fallback: str) -> str:
    emoji_id = get_setting(f"custom_emoji_{name}_id")
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{html.escape(emoji_id)}">{html.escape(fallback)}</tg-emoji>'


def completion_messages() -> tuple[str, str]:
    fallback = "✅ Task completed.\nGreat work 👏"
    decorated = f"{custom_emoji_markup('complete', '✅')} Task completed.\nGreat work {custom_emoji_markup('applause', '👏')}"
    return decorated, fallback


def is_format_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("emoji", "entity", "parse", "format", "unsupported"))


async def reply_completion_message(message, **kwargs) -> None:
    decorated, fallback = completion_messages()
    try:
        if decorated != fallback:
            await message.reply_text(decorated, parse_mode=ParseMode.HTML, **kwargs)
            return
        await message.reply_text(fallback, **kwargs)
    except BadRequest as exc:
        if not is_format_rejection(exc):
            logging.warning("Completion delivery rejected; not retrying fallback: %s", exc)
            return
        await message.reply_text(fallback, **kwargs)
    except Forbidden as exc:
        logging.warning("Completion delivery rejected; not retrying fallback: %s", exc)
    except Exception as exc:
        logging.warning("Completion decoration delivery uncertain; not retrying fallback: %s", type(exc).__name__)


def split_message(text: str, max_len: int = 3800) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    current = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            parts.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        parts.append("\n".join(current))
    return parts


async def send_long_message(chat_id: int, text: str, bot, thread_id: Optional[int] = None) -> None:
    for part in split_message(text):
        await bot.send_message(chat_id=chat_id, text=part, message_thread_id=thread_id)


def feedback_chunks(text: str, max_len: int = 2600) -> list[str]:
    chunks = []
    remaining = text
    while remaining:
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:]
    return chunks or [""]


async def send_feedback_display(
    bot,
    chat_id: int,
    thread_id: Optional[int],
    task: Task,
    feedback_id: int,
    user,
    feedback_text: str,
    created_at: Optional[datetime] = None,
) -> Optional[int]:
    created_local = (created_at or datetime.now(tz=ZoneInfo("UTC"))).astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
    username = f"@{user.username}" if getattr(user, "username", None) else f"user {user.id}"
    chunks = feedback_chunks(feedback_text)
    first_message_id = None
    for index, chunk in enumerate(chunks, start=1):
        part_label = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
        text = (
            f"📝 Feedback #{feedback_id}{part_label}\n"
            f"Task #{task.id}: {html.escape(task.title)}\n"
            f"Submitted by: {html.escape(username)}\n"
            f"Time: {html.escape(created_local)}\n\n"
            f"{html.escape(chunk)}"
        )
        message = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        if first_message_id is None:
            first_message_id = getattr(message, "message_id", None)
        record_feedback_display_chunk(feedback_id, index, getattr(message, "message_id", 0))
    return first_message_id


def set_task_status(task_id: int, chat_id: int, status: str) -> bool:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        if status == "done":
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = 'done',
                    completed_at_utc = COALESCE(completed_at_utc, ?)
                WHERE id = ? AND chat_id = ? AND status != 'done'
                """,
                (now, task_id, chat_id),
            )
            return cur.rowcount == 1
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = ?
            WHERE id = ? AND chat_id = ? AND status != 'done'
            """,
            (status, task_id, chat_id),
        )
        return cur.rowcount == 1

def set_task_assignee(task_id: int, chat_id: int, assignee: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET assignee = ? WHERE id = ? AND chat_id = ? AND status != 'done'",
            (assignee, task_id, chat_id),
        )


def set_setting(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_setting(key: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
        return row["value"] if row else None


def setting_enabled(key: str, default: bool = False) -> bool:
    value = get_setting(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def parse_local_hhmm(value: Optional[str], default: str) -> tuple[int, int]:
    raw = (value or default).strip()
    try:
        hour_str, minute_str = raw.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
    except (TypeError, ValueError):
        hour, minute = [int(part) for part in default.split(":", 1)]
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour, minute = [int(part) for part in default.split(":", 1)]
    return hour, minute


def local_schedule_time(setting_key: str, default: str):
    hour, minute = parse_local_hhmm(get_setting(setting_key), default)
    return datetime.now(tz=TZ).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    ).timetz()


def stale_pending_cutoff(now: Optional[datetime] = None, max_age: timedelta = timedelta(minutes=15)) -> str:
    current = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC"))
    return (current - max_age).isoformat()


def mark_stale_pending_sends_uncertain(now: Optional[datetime] = None) -> int:
    updated_at = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC")).isoformat()
    cutoff = stale_pending_cutoff(now)
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE scheduled_sends
            SET state = 'uncertain',
                updated_at_utc = ?,
                error = 'stale_pending_after_restart'
            WHERE state = 'pending'
              AND claimed_at_utc < ?
            """,
            (updated_at, cutoff),
        )
        return cur.rowcount


def begin_scheduled_send(
    key: str,
    chat_id: Optional[int] = None,
    thread_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> bool:
    claimed_at = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC")).isoformat()
    mark_stale_pending_sends_uncertain(now)
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO scheduled_sends
            (key, chat_id, thread_id, state, claimed_at_utc, updated_at_utc, sent_at_utc, attempts)
            VALUES (?, ?, ?, 'pending', ?, ?, '', 1)
            """,
            (key, chat_id, thread_id, claimed_at, claimed_at),
        )
        if cur.rowcount == 1:
            return True
        row = conn.execute("SELECT state FROM scheduled_sends WHERE key = ?", (key,)).fetchone()
        if row and row["state"] == "failed":
            cur = conn.execute(
                """
                UPDATE scheduled_sends
                SET state = 'pending',
                    chat_id = COALESCE(?, chat_id),
                    thread_id = COALESCE(?, thread_id),
                    claimed_at_utc = ?,
                    updated_at_utc = ?,
                    attempts = attempts + 1,
                    error = NULL
                WHERE key = ? AND state = 'failed'
                """,
                (chat_id, thread_id, claimed_at, claimed_at, key),
            )
            return cur.rowcount == 1
        return False


def mark_scheduled_send_sent(key: str, message_id: Optional[int], now: Optional[datetime] = None) -> None:
    sent_at = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE scheduled_sends
            SET state = 'sent',
                updated_at_utc = ?,
                sent_at_utc = ?,
                message_id = ?,
                error = NULL
            WHERE key = ?
            """,
            (sent_at, sent_at, message_id, key),
        )


def mark_scheduled_send_outcome(key: str, state: str, error: str, now: Optional[datetime] = None) -> None:
    updated_at = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC")).isoformat()
    if state not in {"failed", "uncertain"}:
        raise ValueError("scheduled send state must be failed or uncertain")
    with get_db() as conn:
        conn.execute(
            """
            UPDATE scheduled_sends
            SET state = ?,
                updated_at_utc = ?,
                error = ?
            WHERE key = ?
            """,
            (state, updated_at, error[:500], key),
        )


def resolve_scheduled_send(key: str, chat_id: int, action: str, message_id: Optional[int] = None) -> bool:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scheduled_sends WHERE key = ?", (key,)).fetchone()
        if not row or row["chat_id"] is None or int(row["chat_id"]) != chat_id:
            return False
        if action == "delivered":
            conn.execute(
                """
                UPDATE scheduled_sends
                SET state = 'sent',
                    sent_at_utc = COALESCE(NULLIF(sent_at_utc, ''), ?),
                    updated_at_utc = ?,
                    message_id = COALESCE(?, message_id),
                    error = NULL
                WHERE key = ?
                """,
                (now, now, message_id, key),
            )
            return True
        if action == "notdelivered":
            conn.execute(
                """
                UPDATE scheduled_sends
                SET state = 'failed',
                    updated_at_utc = ?,
                    error = 'admin_confirmed_not_delivered'
                WHERE key = ?
                """,
                (now, key),
            )
            return True
        return False


def classify_send_exception(exc: Exception) -> str:
    if isinstance(exc, (BadRequest, Forbidden)):
        return "failed"
    if isinstance(exc, (TimedOut, NetworkError, RetryAfter, TimeoutError)):
        return "uncertain"
    return "uncertain"


async def send_scheduled_message(
    bot,
    key: str,
    chat_id: int,
    text: str,
    thread_id: Optional[int] = None,
    reply_markup=None,
    now: Optional[datetime] = None,
) -> bool:
    if not begin_scheduled_send(key, chat_id=chat_id, thread_id=thread_id, now=now):
        return False
    try:
        message = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        mark_scheduled_send_outcome(key, classify_send_exception(exc), type(exc).__name__, now)
        return False
    mark_scheduled_send_sent(key, getattr(message, "message_id", None), now)
    return True


def scheduled_send_issues(limit: int = 10) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM scheduled_sends
            WHERE state IN ('pending', 'failed', 'uncertain')
            ORDER BY updated_at_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def create_pending_assignment(
    chat_id: int,
    task_id: int,
    thread_id: Optional[int],
    owner_id: int,
    prompt_message_id: int,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_assignments
            (chat_id, task_id, thread_id, owner_id, prompt_message_id, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                task_id,
                thread_id,
                owner_id,
                prompt_message_id,
                datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            ),
        )


def get_pending_assignment_by_prompt(chat_id: int, prompt_message_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM pending_assignments
            WHERE chat_id = ? AND prompt_message_id = ?
            """,
            (chat_id, prompt_message_id),
        ).fetchone()


def get_pending_assignment_by_task(chat_id: int, task_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM pending_assignments
            WHERE chat_id = ? AND task_id = ?
            """,
            (chat_id, task_id),
        ).fetchone()


def clear_pending_assignment(chat_id: int, task_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM pending_assignments WHERE chat_id = ? AND task_id = ?",
            (chat_id, task_id),
        )


def feedback_cancel_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancel", callback_data=f"feedback_cancel:{request_id}")]]
    )


def create_pending_feedback(
    chat_id: int,
    thread_id: Optional[int],
    task_id: int,
    user_id: int,
    prompt_message_id: int,
    request_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    created = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC"))
    expires = created + timedelta(minutes=30)
    request_id = request_id or f"fb-{task_id}-{user_id}-{int(created.timestamp())}-{random.randint(1000, 9999)}"
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_feedback
            (chat_id, thread_id, task_id, user_id, prompt_message_id, request_id, created_at_utc, expires_at_utc, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                chat_id,
                thread_id,
                task_id,
                user_id,
                prompt_message_id,
                request_id,
                created.isoformat(),
                expires.isoformat(),
            ),
        )
    return request_id


def update_pending_feedback_request(chat_id: int, task_id: int, user_id: int, request_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE pending_feedback
            SET request_id = ?
            WHERE chat_id = ? AND task_id = ? AND user_id = ? AND status = 'pending'
            """,
            (request_id, chat_id, task_id, user_id),
        )


def get_pending_feedback_by_prompt(
    chat_id: int,
    thread_id: Optional[int],
    user_id: int,
    prompt_message_id: int,
) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM pending_feedback
            WHERE chat_id = ?
              AND ((thread_id IS NULL AND ? IS NULL) OR thread_id = ?)
              AND user_id = ?
              AND prompt_message_id = ?
              AND status = 'pending'
            """,
            (chat_id, thread_id, thread_id, user_id, prompt_message_id),
        ).fetchone()


def get_pending_feedback_by_request(request_id: str) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM pending_feedback WHERE request_id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()


def close_pending_feedback(request_id: str, status: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE pending_feedback SET status = ? WHERE request_id = ?",
            (status, request_id),
        )


def save_feedback_entry(
    request_id: str,
    chat_id: int,
    thread_id: Optional[int],
    task_id: int,
    user_id: int,
    username: Optional[str],
    feedback_text: str,
    now: Optional[datetime] = None,
) -> Optional[int]:
    created = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO feedback_entries
            (request_id, chat_id, thread_id, task_id, user_id, username, feedback_text, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (request_id, chat_id, thread_id, task_id, user_id, username or "", feedback_text, created),
        )
        if cur.rowcount != 1:
            return None
        return int(cur.lastrowid)


def set_feedback_display_message(feedback_id: int, message_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE feedback_entries SET display_message_id = ?, display_state = 'sent' WHERE id = ?",
            (message_id, feedback_id),
        )


def set_feedback_display_state(feedback_id: int, state: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE feedback_entries SET display_state = ? WHERE id = ?",
            (state, feedback_id),
        )


def record_feedback_display_chunk(feedback_id: int, chunk_index: int, message_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO feedback_display_chunks
            (feedback_id, chunk_index, message_id, sent_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            (feedback_id, chunk_index, message_id, datetime.now(tz=ZoneInfo("UTC")).isoformat()),
        )


def get_feedback_entry(feedback_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM feedback_entries WHERE id = ?",
            (feedback_id,),
        ).fetchone()


def recent_assignees(chat_id: int, limit: int = 5) -> list[str]:
    assignees: list[str] = []
    default_assignee = get_setting("default_assignee")
    if default_assignee and default_assignee != "@unassigned":
        assignees.append(default_assignee)

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT assignee
            FROM tasks
            WHERE chat_id = ? AND assignee != '@unassigned'
            GROUP BY assignee
            ORDER BY MAX(id) DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()

    for row in rows:
        assignee = row["assignee"]
        if assignee not in assignees:
            assignees.append(assignee)
        if len(assignees) >= limit:
            break
    return assignees


def reserve_fiverr_order(order: FiverrOrder, chat_id: int) -> tuple[Optional[sqlite3.Row], bool]:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO fiverr_orders
            (order_id, chat_id, client_name, source, first_message_id, last_message_id, status, created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)
            """,
            (
                order.order_id,
                chat_id,
                order.client_name,
                order.source,
                order.message_id,
                order.message_id,
                now,
                now,
            ),
        )
        inserted = cursor.rowcount == 1
        conn.execute(
            """
            UPDATE fiverr_orders
            SET last_message_id = COALESCE(NULLIF(?, ''), last_message_id),
                updated_at_utc = ?
            WHERE order_id = ?
            """,
            (order.message_id, now, order.order_id),
        )
        row = conn.execute(
            "SELECT * FROM fiverr_orders WHERE order_id = ?",
            (order.order_id,),
        ).fetchone()
        return row, inserted


def mark_fiverr_order_created(
    order_id: str,
    task_id: int,
    topic_thread_id: Optional[int],
) -> None:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE fiverr_orders
            SET task_id = ?, topic_thread_id = ?, status = 'created', updated_at_utc = ?
            WHERE order_id = ?
            """,
            (task_id, topic_thread_id, now, order_id),
        )


def mark_fiverr_order_quarantined(order_id: str, reason: str) -> None:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE fiverr_orders
            SET status = ?, updated_at_utc = ?
            WHERE order_id = ? AND task_id IS NULL
            """,
            (f"quarantined:{reason}", now, order_id),
        )


def save_inbound_quarantine(
    source: str,
    message_id: str,
    order_id: Optional[str],
    reason: str,
    subject: str,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO inbound_quarantine (source, message_id, order_id, reason, subject, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                message_id,
                order_id,
                reason,
                subject[:500],
                datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            ),
        )


def save_pending_fiverr_requirement_order(order: FiverrOrder, chat_id: int, gmail_thread_id: str) -> None:
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO pending_fiverr_requirement_orders
            (order_id, chat_id, client_name, due_utc, source, first_message_id, gmail_thread_id, created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                client_name = excluded.client_name,
                due_utc = excluded.due_utc,
                source = excluded.source,
                first_message_id = COALESCE(NULLIF(excluded.first_message_id, ''), first_message_id),
                gmail_thread_id = COALESCE(NULLIF(excluded.gmail_thread_id, ''), gmail_thread_id),
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                order.order_id,
                chat_id,
                order.client_name,
                order.due.astimezone(ZoneInfo("UTC")).isoformat(),
                order.source,
                order.message_id,
                gmail_thread_id,
                now,
                now,
            ),
        )


def get_pending_fiverr_requirement_order(order_id: str) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM pending_fiverr_requirement_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()


def get_topic_thread_id(chat_id: int, name: str) -> Optional[int]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT thread_id FROM topics WHERE chat_id = ? AND name = ?",
            (chat_id, name.lower()),
        ).fetchone()
        return int(row["thread_id"]) if row else None


def save_topic(chat_id: int, name: str, thread_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO topics (chat_id, name, thread_id, created_at_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, name) DO UPDATE SET thread_id = excluded.thread_id
            """,
            (
                chat_id,
                name.lower(),
                thread_id,
                datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            ),
        )


def task_ack_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍 All set", callback_data=f"task_ack:{task_id}:yes"),
                InlineKeyboardButton("❓ Need details", callback_data=f"task_ack:{task_id}:no"),
            ]
        ]
    )


def task_delivery_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 Send to Client", callback_data=f"task_delivery:{task_id}:sent"),
                InlineKeyboardButton("🟠 Request Revision", callback_data=f"task_delivery:{task_id}:revision"),
            ]
        ]
    )


def assignment_keyboard(task_id: int, chat_id: int) -> Optional[InlineKeyboardMarkup]:
    buttons = [
        [
            InlineKeyboardButton(
                assignee,
                callback_data=f"task_assign:{task_id}:{assignee.lstrip('@')}",
            )
        ]
        for assignee in recent_assignees(chat_id)
    ]
    return InlineKeyboardMarkup(buttons) if buttons else None


def fiverr_order_url(order_id: str) -> str:
    return f"https://www.fiverr.com/users/funanimation1/manage_orders/{order_id}"


def deadline_detail(deadline_utc: datetime, now: Optional[datetime] = None) -> str:
    now_utc = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC"))
    deadline_utc = deadline_utc.astimezone(ZoneInfo("UTC"))
    remaining = deadline_utc - now_utc
    total_seconds = int(abs(remaining.total_seconds()))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days:
        amount = f"{days}d {hours}h"
    elif hours:
        amount = f"{hours}h {minutes}m"
    else:
        amount = f"{minutes}m"

    local = format_deadline_local(deadline_utc)
    if remaining.total_seconds() < 0:
        return f"{local} — overdue by {amount}"
    return f"{local} — {amount} remaining"


def task_control_panel_text(task: Task) -> str:
    assignee = task.assignee if task.assignee != "@unassigned" else "Unassigned"
    return (
        f"{status_emoji(task.status)} Task #{task.id}\n"
        f"{task.title}\n"
        f"Status: {status_label(task.status)}\n"
        f"Assignee: {assignee}\n"
        f"Deadline: {deadline_detail(task.deadline_utc)}"
    )


def task_control_keyboard(task: Task, chat_id: int, confirm_done: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if confirm_done:
        rows.append(
            [
                InlineKeyboardButton("✅ Yes, complete", callback_data=f"order_ctl:{task.id}:done"),
                InlineKeyboardButton("Cancel", callback_data=f"order_ctl:{task.id}:refresh"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    if task.status not in {"done", "sent_to_client"}:
        if task.assignee != "@unassigned" and task.status in {"assigned", "blocked", "revision"}:
            rows.append(
                [
                    InlineKeyboardButton("Start", callback_data=f"order_ctl:{task.id}:start"),
                    InlineKeyboardButton("Need details", callback_data=f"order_ctl:{task.id}:blocked"),
                ]
            )
        if task.assignee != "@unassigned" and task.status in {"assigned", "in_progress", "revision"}:
            rows.append([InlineKeyboardButton("Mark submitted", callback_data=f"order_ctl:{task.id}:submit")])
        rows.append([InlineKeyboardButton("Complete", callback_data=f"order_ctl:{task.id}:confirm_done")])

    rows.append([InlineKeyboardButton("Deadline", callback_data=f"order_ctl:{task.id}:deadline")])
    rows.append([InlineKeyboardButton("Feedback", callback_data=f"order_ctl:{task.id}:feedback")])

    assignees = recent_assignees(chat_id, limit=1)
    assign_label = "Reassign" if task.assignee != "@unassigned" else "Assign"
    if assignees:
        rows.append([InlineKeyboardButton(assign_label, callback_data=f"order_ctl:{task.id}:assign_menu")])

    order = get_fiverr_order_by_task(task.id, chat_id)
    if order:
        rows.append([InlineKeyboardButton("Open Fiverr", url=fiverr_order_url(order["order_id"]))])

    rows.append([InlineKeyboardButton("Refresh", callback_data=f"order_ctl:{task.id}:refresh")])
    return InlineKeyboardMarkup(rows)


def task_assign_control_keyboard(task: Task, chat_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                assignee,
                callback_data=f"order_assign:{task.id}:{assignee.lstrip('@')}",
            )
        ]
        for assignee in recent_assignees(chat_id)
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"order_ctl:{task.id}:refresh")])
    return InlineKeyboardMarkup(rows)


def order_shortcuts_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[ORDER_CONTROLS_LABEL, DEADLINE_LABEL]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


async def send_task_control_panel(bot, chat_id: int, task: Task) -> None:
    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=task.thread_id,
        text=task_control_panel_text(task),
        reply_markup=task_control_keyboard(task, chat_id),
    )


async def send_order_shortcuts_setup(bot, chat_id: int, thread_id: Optional[int]) -> None:
    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=(
            "Keyboard shortcuts enabled for this chat.\n"
            f"Tap '{ORDER_CONTROLS_LABEL}' for the current topic panel or '{DEADLINE_LABEL}' for the current topic deadline."
        ),
        reply_markup=order_shortcuts_keyboard(),
    )


async def send_assignee_prompt_to_chat(
    bot,
    chat_id: int,
    thread_id: Optional[int],
    task: Task,
) -> None:
    text = (
        f"📌 Task assigned, {task.assignee}.\n"
        "Ready to start, or need more details?"
    )
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=task_ack_keyboard(task.id),
        message_thread_id=thread_id,
    )


async def schedule_task_jobs(application: Application, task: Task) -> None:
    # Schedule reminders 24h and 1h before deadline
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    offsets = [(timedelta(hours=24), "(24h)"), (timedelta(hours=1), "(1h)")]

    for offset, label in offsets:
        run_at = task.deadline_utc - offset
        if run_at <= now_utc:
            continue
        job_name = f"task:{task.id}:{int(offset.total_seconds())}"
        application.job_queue.run_once(
            remind_task_job,
            when=run_at,
            data={
                "task_id": task.id,
                "chat_id": task.chat_id,
                "thread_id": task.thread_id,
                "label": label,
            },
            name=job_name,
        )


def cancel_task_jobs(application: Application, task_id: int) -> None:
    for job in application.job_queue.jobs():
        if job.name and job.name.startswith(f"task:{task_id}:"):
            job.schedule_removal()


async def remind_task_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    task_id = data["task_id"]
    chat_id = data["chat_id"]
    thread_id = data.get("thread_id")
    label = data["label"]

    task = get_task(task_id, chat_id)
    if not task or task.status == "done":
        return

    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=task_reminder_text(task, label),
    )


def build_daily_update_message(tasks: list[Task]) -> str:
    if not tasks:
        return "🧾 Daily update (10 PM PST)\n\n📭 No open tasks right now."

    lines = []
    for t in tasks:
        due = t.deadline_utc.astimezone(TZ).strftime("%b %d, %H:%M")
        assignee = t.assignee if t.assignee != "@unassigned" else "Unassigned"
        lines.append(
            f"{status_emoji(t.status)} #{t.id} — {t.title}\n"
            f"   👤 {assignee} | ⏰ {due}"
        )
    return "🧾 Daily update (10 PM PST)\n\n" + "\n\n".join(lines)


def previous_week_bounds_utc(now: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    local_now = (now or datetime.now(tz=TZ)).astimezone(TZ)
    this_monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    start_local = this_monday - timedelta(days=7)
    end_local = this_monday
    label = f"{start_local.strftime('%b %-d')}–{(end_local - timedelta(days=1)).strftime('%b %-d, %Y')}"
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC")), label


def previous_month_bounds_utc(now: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    local_now = (now or datetime.now(tz=TZ)).astimezone(TZ)
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_end = month_start
    previous_start = (month_start - timedelta(days=1)).replace(day=1)
    label = previous_start.strftime("%B %Y")
    return previous_start.astimezone(ZoneInfo("UTC")), previous_end.astimezone(ZoneInfo("UTC")), label


def completed_project_rows(start_utc: datetime, end_utc: datetime) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT
                t.*,
                fo.order_id AS fiverr_order_id,
                fo.client_name AS fiverr_client_name
            FROM tasks t
            LEFT JOIN fiverr_orders fo
                ON fo.task_id = t.id AND fo.chat_id = t.chat_id AND fo.status = 'created'
            WHERE t.status = 'done'
              AND t.completed_at_utc IS NOT NULL
              AND t.completed_at_utc >= ?
              AND t.completed_at_utc < ?
            ORDER BY t.completed_at_utc ASC, t.id ASC
            """,
            (
                start_utc.astimezone(ZoneInfo("UTC")).isoformat(),
                end_utc.astimezone(ZoneInfo("UTC")).isoformat(),
            ),
        ).fetchall()


def tracking_start_utc() -> Optional[datetime]:
    value = get_setting("tracking_start_utc")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo("UTC"))
    except ValueError:
        return None


def is_fiverr_project_row(row: sqlite3.Row) -> bool:
    return bool(row["fiverr_order_id"]) or (row["title"] or "").lower().startswith("fiverr order #")


def build_completed_projects_report(kind: str, now: Optional[datetime] = None) -> tuple[str, Optional[str]]:
    if kind == "weekly":
        start_utc, end_utc, label = previous_week_bounds_utc(now)
        heading = f"📊 Weekly completed projects — {label}"
    elif kind == "monthly":
        start_utc, end_utc, label = previous_month_bounds_utc(now)
        heading = f"🏁 Monthly completed projects — {label}"
    else:
        raise ValueError("unknown report kind")

    tracking_start = tracking_start_utc()
    if tracking_start and end_utc <= tracking_start:
        return f"{kind}:{start_utc.date().isoformat()}", None
    effective_start = max(start_utc, tracking_start) if tracking_start else start_utc

    rows = completed_project_rows(effective_start, end_utc)
    fiverr_rows = [row for row in rows if is_fiverr_project_row(row)]
    manual_rows = [row for row in rows if not is_fiverr_project_row(row)]

    lines = [
        heading,
        "",
        f"✅ Tracked completions: {len(rows)}",
        f"🛍️ Fiverr order projects: {len(fiverr_rows)}",
        f"🧰 Other tracked tasks: {len(manual_rows)}",
    ]
    if tracking_start:
        tracking_label = tracking_start.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
        if start_utc < tracking_start < end_utc:
            lines.append(
                f"Coverage: tracked completions since {tracking_label}; earlier part of this period is unknown."
            )
        else:
            lines.append(f"Coverage: tracked completions since {tracking_label}.")
        lines.append("Legacy done tasks without completion dates are not counted.")
    lines.append("")

    if fiverr_rows:
        lines.append("Fiverr orders")
        for row in fiverr_rows[:20]:
            order_id = row["fiverr_order_id"] or extract_fiverr_order_id(row["title"] or "") or "unknown order"
            completed_local = datetime.fromisoformat(row["completed_at_utc"]).astimezone(TZ).strftime("%b %-d, %H:%M")
            lines.append(f"• #{row['id']} {order_id} — {row['assignee']} — {completed_local}")
    else:
        lines.append("Fiverr orders: none tracked in the covered window.")

    lines.append("")
    if manual_rows:
        lines.append("Other tracked tasks")
        for row in manual_rows[:20]:
            completed_local = datetime.fromisoformat(row["completed_at_utc"]).astimezone(TZ).strftime("%b %-d, %H:%M")
            lines.append(f"• #{row['id']} {row['title']} — {row['assignee']} — {completed_local}")
    else:
        lines.append("Other tracked tasks: none tracked in the covered window.")

    if len(rows) > 40:
        lines.append(f"\nShowing 40 of {len(rows)} completed items.")
    return f"{kind}:{start_utc.date().isoformat()}", "\n".join(lines)


async def completed_projects_report_job(context: ContextTypes.DEFAULT_TYPE, kind: str, now: Optional[datetime] = None) -> None:
    chat_id = get_setting("general_chat_id")
    thread_id = get_setting("general_thread_id")
    if not chat_id:
        return
    period_key, text = build_completed_projects_report(kind, now)
    if text is None:
        return
    dedupe_key = f"{kind}_report:{period_key}:{chat_id}:{thread_id or 'main'}"
    await send_scheduled_message(
        context.bot,
        dedupe_key,
        int(chat_id),
        text,
        thread_id=int(thread_id) if thread_id else None,
        now=now,
    )


async def weekly_completed_projects_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(tz=TZ)
    if now.weekday() != 0:
        return
    await completed_projects_report_job(context, "weekly", now)


async def monthly_completed_projects_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(tz=TZ)
    if now.day != 1:
        return
    await completed_projects_report_job(context, "monthly", now)


async def catch_up_due_reports(context: ContextTypes.DEFAULT_TYPE, now: Optional[datetime] = None) -> None:
    local_now = (now or datetime.now(tz=TZ)).astimezone(TZ)
    report_hour, report_minute = parse_local_hhmm(get_setting("report_time"), REPORT_TIME_DEFAULT)
    this_monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=report_hour,
        minute=report_minute,
        second=0,
        microsecond=0,
    )
    if local_now >= this_monday:
        await completed_projects_report_job(context, "weekly", local_now)
    first_of_month = local_now.replace(day=1, hour=report_hour, minute=report_minute, second=0, microsecond=0)
    if local_now >= first_of_month:
        await completed_projects_report_job(context, "monthly", local_now)


def nth_weekday_of_month(local_dt: datetime) -> int:
    return ((local_dt.day - 1) // 7) + 1


def should_send_game_invite(local_dt: datetime) -> bool:
    return local_dt.weekday() == 4 and nth_weekday_of_month(local_dt) in {2, 4}


def game_destination() -> Optional[tuple[int, int]]:
    chat_id = get_setting("games_chat_id")
    thread_id = get_setting("games_thread_id")
    if not chat_id or not thread_id or not setting_enabled("games_enabled", default=False):
        return None
    return int(chat_id), int(thread_id)


def next_game_prompt(now: Optional[datetime] = None) -> tuple[str, str, list[str]]:
    prompts = [
        ("Emoji puzzle: movie title — 🧊🚢", "Titanic", ["Titanic", "Frozen", "Ice Age"]),
        ("Quick trivia: what color do you get by mixing blue and yellow?", "Green", ["Green", "Purple", "Orange"]),
        ("Tiny riddle: I speak without a mouth and hear without ears. What am I?", "Echo", ["Echo", "Clock", "Map"]),
    ]
    local_now = (now or datetime.now(tz=TZ)).astimezone(TZ)
    return prompts[local_now.day % len(prompts)]


def create_game_session(chat_id: int, thread_id: int, now: Optional[datetime] = None) -> sqlite3.Row:
    local_now = (now or datetime.now(tz=TZ)).astimezone(TZ)
    prompt, answer, choices = next_game_prompt(local_now)
    session_id = f"{local_now.strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
    created = local_now.astimezone(ZoneInfo("UTC"))
    closes = (local_now + timedelta(minutes=3)).astimezone(ZoneInfo("UTC"))
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO game_sessions (id, chat_id, thread_id, prompt, answer, choices, status, created_at_utc, closes_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (session_id, chat_id, thread_id, prompt, answer, "|".join(choices), created.isoformat(), closes.isoformat()),
        )
        return conn.execute("SELECT * FROM game_sessions WHERE id = ?", (session_id,)).fetchone()


def get_game_session(session_id: str) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute("SELECT * FROM game_sessions WHERE id = ?", (session_id,)).fetchone()


def update_game_session_message(session_id: str, message_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE game_sessions SET message_id = ? WHERE id = ?", (message_id, session_id))


def close_game_session(session_id: str) -> Optional[str]:
    with get_db() as conn:
        session = conn.execute("SELECT * FROM game_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            return None
        rows = conn.execute(
            "SELECT is_correct, COUNT(*) AS count FROM game_answers WHERE session_id = ? GROUP BY is_correct",
            (session_id,),
        ).fetchall()
        correct = sum(row["count"] for row in rows if row["is_correct"])
        total = sum(row["count"] for row in rows)
        summary = f"{correct}/{total} correct"
        conn.execute(
            "UPDATE game_sessions SET status = 'closed', result_summary = ? WHERE id = ?",
            (summary, session_id),
        )
        conn.execute("DELETE FROM game_answers WHERE session_id = ?", (session_id,))
        return summary


def game_invite_keyboard(session: sqlite3.Row) -> InlineKeyboardMarkup:
    choices = [choice for choice in (session["choices"] or "").split("|") if choice]
    rows = [
        [InlineKeyboardButton(choice, callback_data=f"{GAME_CALLBACK_PREFIX}:answer:{session['id']}:{choice}")]
        for choice in choices
    ]
    rows.append([InlineKeyboardButton("Skip", callback_data=f"{GAME_CALLBACK_PREFIX}:skip:{session['id']}")])
    return InlineKeyboardMarkup(rows)


def build_game_invite_text(session: sqlite3.Row) -> str:
    closes_local = datetime.fromisoformat(session["closes_at_utc"]).astimezone(TZ).strftime("%H:%M")
    return (
        "🎲 Tiny Friday game break\n"
        f"{session['prompt']}\n"
        f"Closes around {closes_local}. Totally optional."
    )


async def game_invite_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(tz=TZ)
    if not should_send_game_invite(now):
        return
    destination = game_destination()
    if not destination:
        return
    chat_id, thread_id = destination
    dedupe_key = f"game_invite:{now.date().isoformat()}:{chat_id}:{thread_id}"
    if not begin_scheduled_send(dedupe_key, chat_id=chat_id, thread_id=thread_id, now=now):
        return
    session = create_game_session(chat_id, thread_id, now)
    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=build_game_invite_text(session),
            reply_markup=game_invite_keyboard(session),
        )
    except Exception as exc:
        close_game_session(session["id"])
        mark_scheduled_send_outcome(dedupe_key, classify_send_exception(exc), type(exc).__name__, now)
        return
    update_game_session_message(session["id"], message.message_id)
    mark_scheduled_send_sent(dedupe_key, message.message_id, now)


def record_game_answer(session_id: str, user_id: int, choice: str, now: Optional[datetime] = None) -> bool:
    session = get_game_session(session_id)
    if not session or session["status"] != "open":
        return False
    choices = {item for item in (session["choices"] or "").split("|") if item}
    if choice not in choices:
        return False
    is_correct = int(choice == session["answer"])
    answered_at = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO game_answers (session_id, user_id, choice, is_correct, answered_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, user_id, choice, is_correct, answered_at),
        )
        return cur.rowcount == 1


async def close_expired_game_sessions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_utc = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with get_db() as conn:
        sessions = conn.execute(
            "SELECT * FROM game_sessions WHERE status = 'open' AND closes_at_utc <= ?",
            (now_utc,),
        ).fetchall()
    for session in sessions:
        summary = close_game_session(session["id"])
        if summary:
            await context.bot.send_message(
                chat_id=session["chat_id"],
                message_thread_id=session["thread_id"],
                text=f"🎲 Game closed: {summary}. Answer: {session['answer']}",
            )


async def daily_update_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = get_setting("general_chat_id")
    thread_id = get_setting("general_thread_id")
    if not chat_id:
        return
    tasks = list_open_tasks(int(chat_id))
    text = build_daily_update_message(tasks)
    await context.bot.send_message(
        chat_id=int(chat_id),
        message_thread_id=int(thread_id) if thread_id else None,
        text=text,
    )


def verify_mailgun_signature(timestamp: str, token: str, signature: str) -> bool:
    if not MAILGUN_SIGNING_KEY:
        return False
    msg = f"{timestamp}{token}".encode("utf-8")
    digest = hmac.new(MAILGUN_SIGNING_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def html_to_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def email_search_text(email: InboundEmail) -> str:
    parts = [
        email.subject,
        email.body,
        html_to_text(email.html_body),
        email.html_body,
    ]
    return "\n".join(part for part in parts if part)


def extract_email_name(text: str) -> Optional[str]:
    match = re.search(r"email_name=([A-Za-z0-9_]+)", text or "")
    return match.group(1) if match else None


def parse_inbound_received_at(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
    except (OverflowError, ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC"))


def is_before_fiverr_cutover(email: InboundEmail) -> bool:
    cutover = get_setting("fiverr_cutover_utc")
    if not cutover:
        return False
    cutover_dt = parse_inbound_received_at(cutover)
    received_dt = parse_inbound_received_at(email.received_at)
    if not cutover_dt or not received_dt:
        return False
    return received_dt < cutover_dt


def extract_fiverr_order_id(text: str) -> Optional[str]:
    patterns = [
        r"\b(FO[A-Z0-9]{8,})\b",
        r"(?i)order\s*#\s*([A-Z0-9]{6,})",
        r"(?i)order\s+no\.?\s*([A-Z0-9]{6,})",
        r"#([A-Za-z0-9]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1).upper()
    return None


def clean_client_name(value: str) -> str:
    name = html.unescape(value or "")
    name = re.sub(r"https?://\S+", "", name)
    name = re.sub(r"\s*(?:please review|feels good|is due|due on|with requirements?).*$", "", name, flags=re.I)
    name = re.sub(r"[!.:\s]+$", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "Fiverr Client"


def extract_client_name(subject: str, body: str) -> Optional[str]:
    combined = f"{subject}\n{body}"
    patterns = [
        r"(?i)order from[:\-]\s*(.+)",
        r"(?i)you received an order from\s+(.+)",
        r"(?i)you just received an order from\s+(.+)",
        r"(?i)you'?ve received an order from\s+(.+)",
        r"(?i)great news:\s*you'?ve received an order from\s+(.+)",
        r"(?i)\byour order\s+FO[A-Z0-9]+\s+with\s+(.+?)\s+due\b",
        r"(?i)^(.+?)\s+has sent the requirements and your order\b",
        r"(?i)buyer[:\-]\s*(.+)",
        r"(?i)client[:\-]\s*(.+)",
        r"(?i)from[:\-]\s*(.+)",
    ]
    for line in combined.splitlines():
        line = line.strip()
        if not line:
            continue
        for pat in patterns:
            m = re.search(pat, line)
            if m:
                name = clean_client_name(m.group(1))
                return name
    m = re.search(r"(?i)from\s+(.+)", subject or "")
    if m:
        name = clean_client_name(m.group(1))
        return name
    return None


def extract_due_datetime(body: str, subject: str) -> Optional[datetime]:
    combined = f"{subject}\n{body}"
    due_patterns = [
        r"(?i)\bdue on\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
        r"(?i)\bdue\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
        r"(?i)\bis due\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
    ]
    for pattern in due_patterns:
        m = re.search(pattern, combined)
        if m:
            try:
                dt = date_parser.parse(m.group(1))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TZ)
                return dt
            except (OverflowError, ValueError, TypeError):
                pass
    # Strong pattern: "is due Feb 11, 2026"
    m = re.search(r"(?i)is due\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", combined)
    if m:
        try:
            dt = date_parser.parse(m.group(1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt
        except (OverflowError, ValueError, TypeError):
            pass
    # Fallback: "due Feb 11, 2026"
    m = re.search(r"(?i)due\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", combined)
    if m:
        try:
            dt = date_parser.parse(m.group(1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt
        except (OverflowError, ValueError, TypeError):
            pass
    candidate_lines = []
    for line in body.splitlines():
        if re.search(r"(?i)due|deliver|delivery|deadline", line):
            candidate_lines.append(line)
    candidate_lines.extend([subject])
    now_local = datetime.now(tz=TZ).replace(hour=18, minute=0, second=0, microsecond=0)
    for line in candidate_lines:
        try:
            dt = date_parser.parse(line, fuzzy=True, default=now_local)
        except (OverflowError, ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt
    return None


def extract_order_id(text: str) -> Optional[str]:
    return extract_fiverr_order_id(text)


def classify_fiverr_email(email: InboundEmail, source: str) -> FiverrDecision:
    if is_before_fiverr_cutover(email):
        return FiverrDecision("ignore", "before_fiverr_cutover")

    text = email_search_text(email)
    normalized = normalize_text(text).lower()
    email_name = (extract_email_name(text) or "").lower()
    order_id = extract_fiverr_order_id(text)

    buyer_or_delivery_markers = [
        "gig_order_created_to_buyer",
        "gig_order_delivered_to_buyer",
        "delivered_to_buyer",
        "status of your order",
        "your order no.",
    ]
    if any(marker in email_name or marker in normalized for marker in buyer_or_delivery_markers):
        return FiverrDecision("ignore", "buyer_or_delivery_notification")

    seller_start_email_names = {
        "gig_order_started_seller",
        "gig_order_with_requirements_started_to_seller",
    }
    seller_start_phrases = [
        "you just received an order from",
        "you've just received an order from",
        "you received an order from",
        "you've received an order from",
        "great news: you've received an order from",
    ]
    is_seller_start = (
        email_name in seller_start_email_names
        or any(phrase in normalized for phrase in seller_start_phrases)
    )

    is_requirements_received = (
        email_name == "work_plan_order_requirements_received"
        or (
            "has sent the requirements and your order" in normalized
            and "is now in progress" in normalized
        )
    )
    if not is_seller_start and is_requirements_received:
        if not order_id:
            return FiverrDecision("quarantine", "missing_order_id")
        client_name = extract_client_name(email.subject, text) or "Fiverr Client"
        due = extract_due_datetime(text, email.subject)
        if due:
            return FiverrDecision(
                "create",
                "requirements_received_order_started",
                FiverrOrder(order_id, client_name, due, source, email.message_id, email.received_at),
            )
        return FiverrDecision(
            "create_after_requirements",
            "requirements_received_missing_due",
            FiverrOrder(order_id, client_name, datetime.now(tz=TZ), source, email.message_id, email.received_at),
        )

    waiting_for_requirements_markers = [
        "work_plan_order_created",
        "waiting for requirements",
    ]
    if not is_seller_start and any(marker in email_name or marker in normalized for marker in waiting_for_requirements_markers):
        if order_id:
            client_name = extract_client_name(email.subject, text) or "Fiverr Client"
            due = extract_due_datetime(text, email.subject)
            if due:
                return FiverrDecision(
                    "defer",
                    "waiting_for_requirements",
                    FiverrOrder(order_id, client_name, due, source, email.message_id, email.received_at),
                )
        return FiverrDecision("ignore", "follow_up_without_new_order")

    follow_up_markers = [
        "view_requirements_reminder",
    ]
    if not is_seller_start and any(marker in email_name or marker in normalized for marker in follow_up_markers):
        return FiverrDecision("ignore", "follow_up_without_new_order")

    if not is_seller_start:
        return FiverrDecision("quarantine", "unrecognized_fiverr_template")
    if not order_id:
        return FiverrDecision("quarantine", "missing_order_id")

    client_name = extract_client_name(email.subject, text) or "Fiverr Client"
    due = extract_due_datetime(text, email.subject)
    if not due:
        return FiverrDecision("quarantine", "missing_due_date", FiverrOrder(order_id, client_name, datetime.now(tz=TZ), source, email.message_id, email.received_at))

    return FiverrDecision(
        "create",
        "seller_order_started",
        FiverrOrder(order_id, client_name, due, source, email.message_id, email.received_at),
    )


async def ensure_topic(application: Application, chat_id: int, client_name: str) -> Optional[int]:
    existing = get_topic_thread_id(chat_id, client_name)
    if existing:
        return existing
    try:
        topic = await application.bot.create_forum_topic(chat_id=chat_id, name=client_name)
        save_topic(chat_id, client_name, topic.message_thread_id)
        return topic.message_thread_id
    except Exception as exc:
        logging.warning("Failed to create topic: %s", exc)
        return None


def fiverr_topic_name(order: FiverrOrder) -> str:
    suffix = f" - {order.order_id}"
    max_client_len = max(1, 128 - len(suffix))
    client = order.client_name[:max_client_len].rstrip()
    return f"{client}{suffix}"


async def create_fiverr_order_topic(application: Application, chat_id: int, order: FiverrOrder) -> Optional[int]:
    topic_name = fiverr_topic_name(order)
    try:
        topic = await application.bot.create_forum_topic(chat_id=chat_id, name=topic_name)
        save_topic(chat_id, topic_name, topic.message_thread_id)
        return topic.message_thread_id
    except Exception as exc:
        logging.warning("Failed to create Fiverr order topic for %s: %s", order.order_id, exc)
        return None


async def handle_mailgun_inbound(request: web.Request, application: Application) -> web.Response:
    data = await request.post()
    timestamp = data.get("timestamp", "")
    token = data.get("token", "")
    signature = data.get("signature", "")
    if not verify_mailgun_signature(timestamp, token, signature):
        return web.Response(status=403, text="invalid signature")

    sender = (data.get("sender") or "").lower()
    if MAILGUN_ALLOWED_SENDER and MAILGUN_ALLOWED_SENDER.lower() not in sender:
        return web.Response(status=200, text="ignored")

    subject = data.get("subject", "") or ""
    body = data.get("stripped-text", "") or data.get("body-plain", "") or ""
    email = InboundEmail(
        subject=subject,
        body=body,
        html_body=data.get("stripped-html", "") or data.get("body-html", "") or "",
        sender=sender,
        message_id=data.get("Message-Id", "") or data.get("message-id", "") or "",
    )
    await process_inbound_email(application, email, source="Mailgun")
    return web.Response(status=200, text="ok")


async def process_inbound_order(application: Application, subject: str, body: str, source: str) -> None:
    email = InboundEmail(subject=subject, body=body)
    await process_inbound_email(application, email, source)


async def process_inbound_email(application: Application, email: InboundEmail, source: str) -> None:
    chat_id = get_setting("general_chat_id")
    if not chat_id:
        return
    chat_id_int = int(chat_id)

    owner_id = get_setting("owner_user_id")
    owner_username = get_setting("owner_username") or "@smbath7"
    if not owner_id:
        await application.bot.send_message(
            chat_id=chat_id_int,
            text=f"⚠️ {source} order received, but owner is not set.\n"
                 "Run /task setowner.",
            )
        return

    decision = classify_fiverr_email(email, source)
    order_id = decision.order.order_id if decision.order else extract_fiverr_order_id(email_search_text(email))
    if decision.action == "ignore":
        logging.info("Ignored %s inbound email: %s", source, decision.reason)
        return
    if decision.action == "defer" and decision.order:
        save_pending_fiverr_requirement_order(decision.order, chat_id_int, email.gmail_thread_id)
        logging.info("Deferred %s Fiverr order %s until requirements arrive", source, decision.order.order_id)
        return
    if decision.action == "create_after_requirements" and decision.order:
        pending = get_pending_fiverr_requirement_order(decision.order.order_id)
        if pending:
            decision.order = FiverrOrder(
                order_id=decision.order.order_id,
                client_name=pending["client_name"] or decision.order.client_name,
                due=datetime.fromisoformat(pending["due_utc"]).astimezone(TZ),
                source=source,
                message_id=decision.order.message_id,
                received_at=decision.order.received_at,
            )
            decision = FiverrDecision("create", "requirements_received_order_started", decision.order)
        else:
            save_inbound_quarantine(
                source=source,
                message_id=email.message_id,
                order_id=decision.order.order_id,
                reason="missing_due_date",
                subject=email.subject,
            )
            logging.warning("Quarantined %s inbound email: missing due date for requirements-received order", source)
            return
    if decision.action == "quarantine" or not decision.order:
        save_inbound_quarantine(
            source=source,
            message_id=email.message_id,
            order_id=order_id,
            reason=decision.reason,
            subject=email.subject,
        )
        logging.warning("Quarantined %s inbound email: %s", source, decision.reason)
        return

    order = decision.order
    row, inserted = reserve_fiverr_order(order, chat_id_int)
    if row and row["task_id"]:
        logging.info("Duplicate Fiverr order %s ignored; task already exists", order.order_id)
        return
    if row and not inserted:
        save_inbound_quarantine(
            source=source,
            message_id=email.message_id,
            order_id=order.order_id,
            reason="uncertain_create_retry",
            subject=email.subject,
        )
        mark_fiverr_order_quarantined(order.order_id, "uncertain_create_retry")
        logging.warning("Quarantined retry for incompletely created Fiverr order %s", order.order_id)
        return

    deadline_local = (order.due - timedelta(days=1)).astimezone(TZ)
    deadline_utc = deadline_local.astimezone(ZoneInfo("UTC"))

    title = f"Fiverr order #{order.order_id}"

    thread_id = await create_fiverr_order_topic(application, chat_id_int, order)
    if thread_id is None:
        save_inbound_quarantine(
            source=source,
            message_id=email.message_id,
            order_id=order.order_id,
            reason="topic_create_failed",
            subject=email.subject,
        )
        mark_fiverr_order_quarantined(order.order_id, "topic_create_failed")
        return

    task_id = save_task(
        title=title,
        assignee="@unassigned",
        deadline_utc=deadline_utc,
        creator_id=int(owner_id),
        chat_id=chat_id_int,
        thread_id=thread_id,
    )
    mark_fiverr_order_created(order.order_id, task_id, thread_id)

    task = get_task(task_id, chat_id_int)
    if task:
        await schedule_task_jobs(application, task)
        await application.bot.send_message(
            chat_id=chat_id_int,
            message_thread_id=thread_id,
            text=f"🟡 Task #{task_id} created from {source}.\n"
                 f"👤 Unassigned | ⏰ {format_deadline_local(task.deadline_utc)}",
            reply_markup=order_shortcuts_keyboard(),
        )
        await send_task_control_panel(application.bot, chat_id_int, task)
        assign_keyboard = assignment_keyboard(task_id, chat_id_int)
        assign_text = (
            f"{owner_username}, who should be assigned? Tap a name or reply with @username."
            if assign_keyboard
            else f"{owner_username}, who should be assigned? Reply with @username."
        )
        prompt = await application.bot.send_message(
            chat_id=chat_id_int,
            message_thread_id=thread_id,
            text=assign_text,
            reply_markup=assign_keyboard,
        )
        create_pending_assignment(
            chat_id=chat_id_int,
            task_id=task_id,
            thread_id=thread_id,
            owner_id=int(owner_id),
            prompt_message_id=prompt.message_id,
        )


async def handle_gmail_webhook(request: web.Request, application: Application) -> web.Response:
    if not GMAIL_WEBHOOK_TOKEN:
        return web.Response(status=403, text="missing token")
    token = request.headers.get("X-Webhook-Token") or request.query.get("token", "")
    if token != GMAIL_WEBHOOK_TOKEN:
        return web.Response(status=403, text="invalid token")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    subject = payload.get("subject", "") or ""
    body = payload.get("body", "") or payload.get("text", "") or payload.get("plainBody", "") or ""
    email = InboundEmail(
        subject=subject,
        body=body,
        html_body=payload.get("htmlBody", "") or payload.get("html", "") or "",
        sender=payload.get("from", "") or payload.get("sender", "") or "",
        message_id=payload.get("messageId", "") or payload.get("id", "") or "",
        gmail_thread_id=payload.get("threadId", "") or "",
        received_at=payload.get("date", "") or payload.get("receivedAt", "") or "",
    )
    await process_inbound_email(application, email, source="Gmail")
    return web.Response(status=200, text="ok")


async def start_webserver(application: Application) -> None:
    web_app = web.Application()
    web_app.router.add_post("/mailgun/inbound", lambda request: handle_mailgun_inbound(request, application))
    web_app.router.add_post("/gmail/inbound", lambda request: handle_gmail_webhook(request, application))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    application.bot_data["web_runner"] = runner


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Task bot is running. Use /task help for commands."
    )

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    messages = [
        "🎉 Approved! Great work! ✅",
        "👏 Congrats! Approved and ready to go! 🚀",
        "✅ Approved! Fantastic job! ✨",
        "🥳 Awesome! Approved! Let’s ship it! 🚢",
        "💯 Approved! You nailed it! 🔥",
    ]
    await update.message.reply_text(random.choice(messages))


def current_thread_id(update: Update) -> Optional[int]:
    return update.effective_message.message_thread_id if update.effective_message else None


def format_task_selector(tasks: list[Task]) -> str:
    lines = [
        f"{status_emoji(t.status)} #{t.id} — {t.title} ({status_label(t.status)})"
        for t in tasks
    ]
    return "Multiple open tasks in this topic. Use /order <id>:\n" + "\n".join(lines)


async def resolve_single_topic_task(update: Update) -> Optional[Task]:
    if not update.effective_chat:
        return None
    tasks = list_open_tasks_by_thread(update.effective_chat.id, current_thread_id(update))
    if not tasks:
        await update.message.reply_text(
            "📭 No open order task in this topic.",
            reply_markup=order_shortcuts_keyboard(),
        )
        return None
    if len(tasks) > 1:
        await update.message.reply_text(
            format_task_selector(tasks),
            reply_markup=order_shortcuts_keyboard(),
        )
        return None
    return tasks[0]


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        try:
            task_id = int(parts[1].strip())
        except ValueError:
            await update.message.reply_text("ℹ️ Usage: /order or /order <task_id>")
            return
        task = get_task(task_id, update.effective_chat.id)
        if not task:
            await update.message.reply_text("❌ Task not found.")
            return
        await send_task_control_panel(context.bot, update.effective_chat.id, task)
        await send_order_shortcuts_setup(context.bot, update.effective_chat.id, task.thread_id)
        return

    task = await resolve_single_topic_task(update)
    if not task:
        return

    await send_task_control_panel(context.bot, update.effective_chat.id, task)
    await send_order_shortcuts_setup(context.bot, update.effective_chat.id, task.thread_id)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    await send_order_shortcuts_setup(context.bot, update.effective_chat.id, current_thread_id(update))


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) == 1:
        await update.message.reply_text("ℹ️ Usage: /feedback <feedback_id>")
        return
    try:
        feedback_id = int(parts[1].strip())
    except ValueError:
        await update.message.reply_text("ℹ️ Usage: /feedback <feedback_id>")
        return
    entry = get_feedback_entry(feedback_id)
    if not entry:
        await update.message.reply_text("❌ Feedback not found.")
        return
    thread_id = current_thread_id(update)
    if entry["chat_id"] != update.effective_chat.id or entry["thread_id"] != thread_id:
        await update.message.reply_text("⚠️ That feedback belongs to a different topic.")
        return
    task = get_task(entry["task_id"], update.effective_chat.id)
    if not task:
        await update.message.reply_text("❌ Task not found.")
        return
    if not await user_can_manage_task(update.effective_user.id, task, context):
        await update.message.reply_text("🛡️ Only the task creator or an admin can view feedback.")
        return
    first_message_id = await send_feedback_display(
        context.bot,
        update.effective_chat.id,
        thread_id,
        task,
        entry["id"],
        SimpleNamespace(id=entry["user_id"], username=entry["username"] or None),
        entry["feedback_text"],
        datetime.fromisoformat(entry["created_at_utc"]).astimezone(ZoneInfo("UTC")),
    )
    if first_message_id is not None:
        set_feedback_display_message(entry["id"], first_message_id)


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    args = update.message.text.split(maxsplit=1)
    sub = args[1].strip().lower() if len(args) > 1 else "help"
    if sub == "help":
        await update.message.reply_text(
            "Games setup:\n"
            "/games settopic — use this topic for optional games\n"
            "/games settime HH:MM — set invitation time\n"
            "/games enable — turn scheduled game invitations on\n"
            "/games disable — turn scheduled game invitations off\n"
            "/games status"
        )
        return
    if not await is_admin(update, context):
        await update.message.reply_text("🛡️ Only admins can manage games.")
        return
    if sub == "settopic":
        thread_id = current_thread_id(update)
        if thread_id is None:
            await update.message.reply_text("⚠️ Run this inside the dedicated Games topic.")
            return
        set_setting("games_chat_id", str(update.effective_chat.id))
        set_setting("games_thread_id", str(thread_id))
        await update.message.reply_text("✅ Games topic saved. Use /games enable when ready.")
        return
    if sub == "enable":
        if not get_setting("games_chat_id") or not get_setting("games_thread_id"):
            await update.message.reply_text("⚠️ Set the Games topic first with /games settopic.")
            return
        set_setting("games_enabled", "1")
        await update.message.reply_text("✅ Optional game invitations enabled for the configured Games topic.")
        return
    if sub.startswith("settime"):
        try:
            parts = shlex.split(sub)
            hour, minute = parse_local_hhmm(parts[1], GAME_INVITE_TIME_DEFAULT)
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /games settime HH:MM")
            return
        set_setting("game_invite_time", f"{hour:02d}:{minute:02d}")
        await update.message.reply_text(
            f"✅ Game invitation time set to {hour:02d}:{minute:02d} Los Angeles time. It applies after the bot restarts."
        )
        return
    if sub == "disable":
        set_setting("games_enabled", "0")
        await update.message.reply_text("✅ Optional game invitations disabled.")
        return
    if sub == "status":
        destination = game_destination()
        if destination:
            await update.message.reply_text(f"🎲 Games enabled in topic {destination[1]}.")
        else:
            await update.message.reply_text("🎲 Games are not enabled or no Games topic is configured.")
        return
    await update.message.reply_text("❓ Unknown games command. Use /games help.")


async def handle_order_shortcut_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.effective_chat:
        return False
    label = (update.message.text or "").strip()
    if label not in {ORDER_CONTROLS_LABEL, DEADLINE_LABEL}:
        return False

    task = await resolve_single_topic_task(update)
    if not task:
        return True

    if label == ORDER_CONTROLS_LABEL:
        await send_task_control_panel(context.bot, update.effective_chat.id, task)
        return True

    await update.message.reply_text(
        f"⏰ Task #{task.id} deadline:\n{deadline_detail(task.deadline_utc)}",
        reply_markup=order_shortcuts_keyboard(),
    )
    return True


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    args = update.message.text.split(maxsplit=1)
    if len(args) == 1:
        await update.message.reply_text("ℹ️ Usage: /task help")
        return

    sub = args[1].strip()
    if sub.startswith("help"):
        await update.message.reply_text(
            "Commands:\n"
            "/task add \"Title\" @user YYYY-MM-DD HH:MM\n"
            "/task list\n"
            "/task listall\n"
            "/task view <id>\n"
            "/task done <id>\n"
            "/task doneall\n"
            "/task doneallall\n"
            "/task submit <id>\n"
            "/task find \"keyword\"\n"
            "/task status\n"
            "/task setgeneral\n"
            "/task setassignee @user\n"
            "/task setowner\n"
            "/task setemoji complete|applause <emoji_id|off>\n"
            "/task setreporttime HH:MM\n"
            "/task sendissues\n"
            "/task sendresolve <key> delivered <message_id>\n"
            "/task sendresolve <key> notdelivered\n"
            "/order or /order <id>\n"
            "/feedback <feedback_id>\n"
            "/menu\n"
        )
        return

    if sub.startswith("add"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can create tasks.")
            return

        try:
            # Use shlex to preserve quoted title
            parts = shlex.split(sub)
            # parts[0] == 'add'
            title = parts[1]
            assignee = parts[2]
            date_str = parts[3]
            time_str = parts[4]
        except Exception:
            await update.message.reply_text("⚠️ Format: /task add \"Title\" @user YYYY-MM-DD HH:MM")
            return

        if not assignee.startswith("@"): 
            await update.message.reply_text("⚠️ Assignee must be a @username.")
            return

        try:
            deadline_utc = parse_deadline(date_str, time_str)
        except ValueError:
            await update.message.reply_text("⏱️ Invalid date/time. Use YYYY-MM-DD HH:MM")
            return

        task_id = save_task(
            title=title,
            assignee=assignee,
            deadline_utc=deadline_utc,
            creator_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            thread_id=update.effective_message.message_thread_id
            if update.effective_message
            else None,
        )

        task = get_task(task_id, update.effective_chat.id)
        if task:
            await schedule_task_jobs(context.application, task)
            await send_assignee_prompt_to_chat(
                context.bot,
                update.effective_chat.id,
                task.thread_id,
                task,
            )

        await update.message.reply_text(
            f"🟡 Task #{task_id} created. Deadline {format_deadline_local(deadline_utc)}",
            reply_markup=task_control_keyboard(task, update.effective_chat.id) if task else None,
        )
        return

    if sub.startswith("listall"):
        tasks = list_open_tasks(update.effective_chat.id)
        if not tasks:
            await update.message.reply_text("📭 No open tasks.")
            return
        lines = [
            f"{status_emoji(t.status)} #{t.id} — {t.title} ({status_label(t.status)})"
            for t in tasks
        ]
        await send_long_message(
            update.effective_chat.id,
            "🗂 All open tasks:\n" + "\n".join(lines),
            context.bot,
            update.effective_message.message_thread_id if update.effective_message else None,
        )
        return

    if sub.startswith("list"):
        # /task list => current topic only
        thread_id = update.effective_message.message_thread_id if update.effective_message else None
        tasks = list_open_tasks_by_thread(update.effective_chat.id, thread_id)
        if not tasks:
            await update.message.reply_text("📭 No open tasks.")
            return
        lines = [
            f"{status_emoji(t.status)} #{t.id} — {t.title} (Due: {t.deadline_utc.astimezone(TZ).strftime('%b %d')})"
            for t in tasks
        ]
        await send_long_message(
            update.effective_chat.id,
            "📋 Open tasks in this topic:\n" + "\n".join(lines),
            context.bot,
            update.effective_message.message_thread_id if update.effective_message else None,
        )
        return

    if sub.startswith("find"):
        try:
            parts = shlex.split(sub)
            query = parts[1]
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /task find \"keyword\"")
            return
        tasks = find_tasks_by_title(update.effective_chat.id, query)
        if not tasks:
            await update.message.reply_text("🔍 No matching open tasks.")
            return
        lines = [
            f"{status_emoji(t.status)} #{t.id} — {t.title} ({status_label(t.status)})"
            for t in tasks
        ]
        await send_long_message(
            update.effective_chat.id,
            "Matches:\n" + "\n".join(lines),
            context.bot,
            update.effective_message.message_thread_id if update.effective_message else None,
        )
        return

    if sub.startswith("view"):
        try:
            task_id = int(sub.split()[1])
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /task view <id>")
            return
        task = get_task(task_id, update.effective_chat.id)
        if not task:
            await update.message.reply_text("❌ Task not found.")
            return
        assignee = task.assignee if task.assignee != "@unassigned" else "Unassigned"
        await update.message.reply_text(
            f"Task #{task.id}\n"
            f"Title: {task.title}\n"
            f"Assignee: {assignee}\n"
            f"Deadline: {format_deadline_local(task.deadline_utc)}\n"
            f"Status: {status_emoji(task.status)} {status_label(task.status)}"
        )
        return

    if sub.startswith("doneallall"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can close tasks.")
            return
        count = mark_all_tasks_done_global(update.effective_chat.id)
        await update.message.reply_text(f"✅ Completed {count} task(s) across all topics.")
        return

    if sub.startswith("doneall"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can close tasks.")
            return
        thread_id = update.effective_message.message_thread_id if update.effective_message else None
        count = mark_all_tasks_done(update.effective_chat.id, thread_id)
        await update.message.reply_text(f"✅ Completed {count} task(s).")
        return

    if sub.startswith("done"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can close tasks.")
            return
        try:
            task_id = int(sub.split()[1])
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /task done <id>")
            return
        task, transitioned = mark_task_done(task_id, update.effective_chat.id)
        if not task:
            await update.message.reply_text("❌ Task not found.")
            return
        if not transitioned:
            await update.message.reply_text("✅ This task is already completed.")
            return
        cancel_task_jobs(context.application, task_id)
        await reply_completion_message(update.message)
        return

    if sub.startswith("submit"):
        if not update.effective_user:
            return
        try:
            task_id = int(sub.split()[1])
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /task submit <id>")
            return
        task = get_task(task_id, update.effective_chat.id)
        if not task:
            await update.message.reply_text("❌ Task not found.")
            return
        if task.status == "done":
            await update.message.reply_text("✅ This task is already completed.")
            return
        if task.assignee == "@unassigned":
            await update.message.reply_text("⚠️ This task is not assigned yet.")
            return
        assignee_username = task.assignee.lstrip("@").lower()
        if (update.effective_user.username or "").lower() != assignee_username:
            await update.message.reply_text("🧑‍💻 Only the assignee can submit this task.")
            return
        await update.message.reply_text(
            "🟣 Task submitted.\nChoose next step:",
            reply_markup=task_delivery_keyboard(task.id),
            message_thread_id=task.thread_id,
        )
        if not set_task_status(task.id, update.effective_chat.id, "submitted"):
            await update.message.reply_text("✅ This task is already completed.")
        return

    if sub.startswith("status"):
        await update.message.reply_text(
            "🟡 Assigned | 🔵 In Progress | 🟣 Submitted\n"
            "🟠 Revision | 🟢 Sent to Client | ✅ Done | 🔴 Blocked"
        )
        return

    if sub.startswith("sendissues"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can view scheduled send issues.")
            return
        rows = scheduled_send_issues(limit=10)
        if not rows:
            await update.message.reply_text("✅ No scheduled send issues.")
            return
        lines = ["Scheduled send issues:"]
        for row in rows:
            lines.append(
                f"• {row['key']} — {row['state']} — attempts {row['attempts']} — {row['error'] or 'no error'}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    if sub.startswith("sendresolve"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can resolve scheduled send issues.")
            return
        try:
            parts = shlex.split(sub)
            key = parts[1]
            action = parts[2].lower()
            message_id = int(parts[3]) if action == "delivered" and len(parts) > 3 else None
        except Exception:
            await update.message.reply_text(
                "ℹ️ Usage: /task sendresolve <key> delivered <message_id> OR /task sendresolve <key> notdelivered"
            )
            return
        if action == "delivered" and message_id is None:
            await update.message.reply_text("⚠️ Provide the delivered Telegram message id.")
            return
        if action not in {"delivered", "notdelivered"}:
            await update.message.reply_text("⚠️ Action must be delivered or notdelivered.")
            return
        if not resolve_scheduled_send(key, update.effective_chat.id, action, message_id):
            await update.message.reply_text("❌ Scheduled send not found for this chat.")
            return
        await update.message.reply_text("✅ Scheduled send issue updated.")
        return

    if sub.startswith("setgeneral"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can set the General topic.")
            return
        thread_id = update.effective_message.message_thread_id if update.effective_message else None
        set_setting("general_chat_id", str(update.effective_chat.id))
        if thread_id is None:
            set_setting("general_thread_id", "")
            await update.message.reply_text("✅ Daily updates will be posted in the main chat.")
        else:
            set_setting("general_thread_id", str(thread_id))
            await update.message.reply_text("✅ General topic set for daily updates.")
        return

    if sub.startswith("setassignee"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can set the default assignee.")
            return
        try:
            parts = shlex.split(sub)
            assignee = parts[1]
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /task setassignee @user")
            return
        if not assignee.startswith("@"):
            await update.message.reply_text("⚠️ Assignee must be a @username.")
            return
        set_setting("default_assignee", assignee)
        await update.message.reply_text(f"✅ Default assignee set to {assignee}")
        return

    if sub.startswith("setowner"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can set the owner.")
            return
        if not update.effective_user:
            return
        set_setting("owner_user_id", str(update.effective_user.id))
        if update.effective_user.username:
            set_setting("owner_username", f"@{update.effective_user.username}")
        await update.message.reply_text("✅ Owner set for automated tasks.")
        return

    if sub.startswith("setreporttime"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can set report time.")
            return
        try:
            parts = shlex.split(sub)
            hour, minute = parse_local_hhmm(parts[1], REPORT_TIME_DEFAULT)
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /task setreporttime HH:MM")
            return
        set_setting("report_time", f"{hour:02d}:{minute:02d}")
        await update.message.reply_text(
            f"✅ Report time set to {hour:02d}:{minute:02d} Los Angeles time. It applies after the bot restarts."
        )
        return

    if sub.startswith("setemoji"):
        if not await is_admin(update, context):
            await update.message.reply_text("🛡️ Only admins can set custom emoji.")
            return
        try:
            parts = shlex.split(sub)
            name = parts[1].lower()
            emoji_id = parts[2]
        except Exception:
            await update.message.reply_text("ℹ️ Usage: /task setemoji complete|applause <emoji_id|off>")
            return
        if name not in {"complete", "applause"}:
            await update.message.reply_text("⚠️ Supported custom emoji names: complete, applause.")
            return
        if emoji_id.lower() == "off":
            set_setting(f"custom_emoji_{name}_id", "")
            await update.message.reply_text(f"✅ Custom emoji for {name} disabled.")
            return
        set_setting(f"custom_emoji_{name}_id", emoji_id)
        await update.message.reply_text(
            f"✅ Custom emoji for {name} saved. If Telegram rejects it, the bot will use normal emoji."
        )
        return

    await update.message.reply_text("❓ Unknown subcommand. Use /task help.")


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_user:
        return
    query = update.callback_query
    data = query.data or ""
    if data.startswith("order_ctl:"):
        await handle_order_control_callback(update, context)
        return

    if data.startswith("order_assign:"):
        await handle_order_assign_callback(update, context)
        return

    if data.startswith("feedback_cancel:"):
        await handle_feedback_cancel_callback(update, context)
        return

    if data.startswith(f"{GAME_CALLBACK_PREFIX}:"):
        await handle_game_callback(update, context)
        return

    if data.startswith("task_ack:"):
        try:
            _, task_id_str, choice = data.split(":", 2)
            task_id = int(task_id_str)
        except Exception:
            await query.answer("⚠️ Invalid action.", show_alert=True)
            return

        task = get_task(task_id, query.message.chat_id if query.message else 0)
        if not task:
            await query.answer("❌ Task not found.", show_alert=True)
            return
        if task.status == "done":
            await query.answer("This task is already completed.", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            return

        # Ensure only the assignee can answer
        assignee_username = task.assignee.lstrip("@").lower()
        if (update.effective_user.username or "").lower() != assignee_username:
            await query.answer("🧑‍💻 Only the assignee can respond.", show_alert=True)
            return

        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)

        if choice == "yes":
            if not set_task_status(task.id, task.chat_id, "in_progress"):
                await query.answer("This task is already completed.", show_alert=True)
                return
            await query.message.reply_text("🔵 Task in progress.\nGood luck 🚀")
            return

        if choice == "no":
            if not set_task_status(task.id, task.chat_id, "blocked"):
                await query.answer("This task is already completed.", show_alert=True)
                return
            creator_mention = f'<a href="tg://user?id={task.creator_id}">task creator</a>'
            await query.message.reply_text(
                f"🔴 Task blocked.\nWaiting for more details from {creator_mention} 🤔",
                parse_mode=ParseMode.HTML,
            )
            return

        return

    if data.startswith("task_delivery:"):
        try:
            _, task_id_str, choice = data.split(":", 2)
            task_id = int(task_id_str)
        except Exception:
            await query.answer("⚠️ Invalid action.", show_alert=True)
            return

        task = get_task(task_id, query.message.chat_id if query.message else 0)
        if not task:
            await query.answer("❌ Task not found.", show_alert=True)
            return
        if task.status == "done":
            await query.answer("This task is already completed.", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            return

        if update.effective_user.id != task.creator_id:
            await query.answer("🛡️ Only the task creator can choose this.", show_alert=True)
            return

        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)

        if choice == "sent":
            if not set_task_status(task.id, task.chat_id, "sent_to_client"):
                await query.answer("This task is already completed.", show_alert=True)
                return
            await query.message.reply_text(
                f"🟢 Sent to client.\n{task.assignee} Fingers crossed 🤞"
            )
            return

        if choice == "revision":
            if not set_task_status(task.id, task.chat_id, "revision"):
                await query.answer("This task is already completed.", show_alert=True)
                return
            creator_mention = f'<a href="tg://user?id={task.creator_id}">task creator</a>'
            await query.message.reply_text(
                f"🟠 Revision requested.\n{creator_mention} please add feedback 📝",
                parse_mode=ParseMode.HTML,
            )
            return

        return

    if data.startswith("task_assign:"):
        try:
            _, task_id_str, username = data.split(":", 2)
            task_id = int(task_id_str)
        except Exception:
            await query.answer("⚠️ Invalid assignment.", show_alert=True)
            return

        chat_id = query.message.chat_id if query.message else 0
        pending = get_pending_assignment_by_task(chat_id, task_id)
        if not pending:
            await query.answer("Assignment already handled.", show_alert=True)
            return
        if update.effective_user.id != pending["owner_id"]:
            await query.answer("🛡️ Only the owner can assign this.", show_alert=True)
            return

        assignee = f"@{username}"
        task = get_task(task_id, chat_id)
        if not task:
            await query.answer("❌ Task not found.", show_alert=True)
            return
        if task.status == "done":
            clear_pending_assignment(chat_id, task_id)
            await query.answer("This task is already completed.", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            return

        set_task_assignee(task_id, chat_id, assignee)
        clear_pending_assignment(chat_id, task_id)
        task = get_task(task_id, chat_id)
        if not task:
            await query.answer("❌ Task not found.", show_alert=True)
            return

        await query.answer(f"Assigned to {assignee}")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ Assigned to {assignee}.",
            message_thread_id=pending["thread_id"],
        )
        await send_assignee_prompt_to_chat(
            context.bot,
            chat_id,
            pending["thread_id"],
            task,
        )
        return


async def on_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    if await handle_order_shortcut_message(update, context):
        return
    reply_to = update.message.reply_to_message
    if not reply_to:
        return
    pending_feedback = get_pending_feedback_by_prompt(
        update.effective_chat.id,
        update.effective_message.message_thread_id if update.effective_message else None,
        update.effective_user.id,
        reply_to.message_id,
    )
    if pending_feedback:
        task = get_task(pending_feedback["task_id"], update.effective_chat.id)
        if not task:
            close_pending_feedback(pending_feedback["request_id"], "cancelled")
            await update.message.reply_text("❌ Task not found.")
            return
        if task.thread_id != pending_feedback["thread_id"]:
            await update.message.reply_text("⚠️ This feedback form belongs to a different topic.")
            return
        expires_at = datetime.fromisoformat(pending_feedback["expires_at_utc"]).astimezone(ZoneInfo("UTC"))
        if expires_at <= datetime.now(tz=ZoneInfo("UTC")):
            close_pending_feedback(pending_feedback["request_id"], "expired")
            await update.message.reply_text("⌛ Feedback form expired. Tap Feedback again to start over.")
            return
        if not await user_can_manage_task(update.effective_user.id, task, context):
            await update.message.reply_text("🛡️ Only the task creator or an admin can submit feedback.")
            return
        feedback_text = (update.message.text or "").strip()
        if not feedback_text:
            await update.message.reply_text("⚠️ Please send feedback as text.")
            return
        feedback_id = save_feedback_entry(
            request_id=pending_feedback["request_id"],
            chat_id=update.effective_chat.id,
            thread_id=task.thread_id,
            task_id=task.id,
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            feedback_text=feedback_text,
        )
        if feedback_id is None:
            await update.message.reply_text("✅ Feedback was already saved.")
            return
        try:
            first_message_id = await send_feedback_display(
                context.bot,
                update.effective_chat.id,
                task.thread_id,
                task,
                feedback_id,
                update.effective_user,
                feedback_text,
            )
        except Exception as exc:
            set_feedback_display_state(
                feedback_id,
                "display_failed" if classify_send_exception(exc) == "failed" else "display_uncertain",
            )
            close_pending_feedback(
                pending_feedback["request_id"],
                "display_failed" if classify_send_exception(exc) == "failed" else "display_uncertain",
            )
            return
        if first_message_id is not None:
            set_feedback_display_message(feedback_id, first_message_id)
        close_pending_feedback(pending_feedback["request_id"], "submitted")
        return

    pending = get_pending_assignment_by_prompt(update.effective_chat.id, reply_to.message_id)
    if not pending:
        return
    if update.effective_user.id != pending["owner_id"]:
        return

    match = re.search(r"@([A-Za-z0-9_]{5,})", update.message.text or "")
    if not match:
        await update.message.reply_text("⚠️ Please reply with a valid @username.")
        return
    assignee = f"@{match.group(1)}"

    task = get_task(pending["task_id"], update.effective_chat.id)
    if not task:
        await update.message.reply_text("❌ Task not found.")
        return
    if task.status == "done":
        clear_pending_assignment(update.effective_chat.id, pending["task_id"])
        await update.message.reply_text("✅ This task is already completed.")
        return

    set_task_assignee(pending["task_id"], update.effective_chat.id, assignee)
    clear_pending_assignment(update.effective_chat.id, pending["task_id"])
    task = get_task(pending["task_id"], update.effective_chat.id)
    if not task:
        await update.message.reply_text("❌ Task not found.")
        return

    await update.message.reply_text(
        f"✅ Assigned to {assignee}.",
        message_thread_id=pending["thread_id"],
    )
    await send_assignee_prompt_to_chat(
        context.bot,
        update.effective_chat.id,
        pending["thread_id"],
        task,
    )


async def handle_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    try:
        _, action, session_id, choice = (query.data or "").split(":", 3)
    except ValueError:
        try:
            _, action, session_id = (query.data or "").split(":", 2)
            choice = ""
        except ValueError:
            await query.answer("Invalid game action.", show_alert=True)
            return

    session = get_game_session(session_id)
    if not session:
        await query.answer("That game is no longer available.", show_alert=True)
        return
    if callback_chat_id(query) != session["chat_id"] or callback_thread_id(query) != session["thread_id"]:
        await query.answer("This game belongs in the Games topic.", show_alert=True)
        return
    if session["message_id"] and query.message and query.message.message_id != session["message_id"]:
        await query.answer("This game button is from another message.", show_alert=True)
        return
    closes_at = datetime.fromisoformat(session["closes_at_utc"]).astimezone(ZoneInfo("UTC"))
    if session["status"] != "open" or closes_at <= datetime.now(tz=ZoneInfo("UTC")):
        summary = close_game_session(session_id)
        await query.answer(summary or "This game is closed.", show_alert=True)
        return
    if action == "skip":
        await query.answer("Skipped. See you next round.")
        return
    if action != "answer":
        await query.answer("Invalid game action.", show_alert=True)
        return
    choices = {item for item in (session["choices"] or "").split("|") if item}
    if choice not in choices:
        await query.answer("That answer is not valid for this game.", show_alert=True)
        return
    if not record_game_answer(session_id, update.effective_user.id, choice):
        await query.answer("You already answered this round.", show_alert=True)
        return
    if choice == session["answer"]:
        await query.answer("Correct! 🎉")
    else:
        await query.answer("Answer saved.")


async def handle_feedback_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    try:
        _, request_id = (query.data or "").split(":", 1)
    except ValueError:
        await query.answer("Invalid feedback action.", show_alert=True)
        return
    pending = get_pending_feedback_by_request(request_id)
    if not pending:
        await query.answer("Feedback request is no longer active.", show_alert=True)
        return
    if callback_chat_id(query) != pending["chat_id"] or callback_thread_id(query) != pending["thread_id"]:
        await query.answer("This feedback request belongs to another topic.", show_alert=True)
        return
    if update.effective_user.id != pending["user_id"]:
        await query.answer("Only the user who opened this feedback form can cancel it.", show_alert=True)
        return
    close_pending_feedback(request_id, "cancelled")
    await query.answer("Cancelled")
    await query.edit_message_reply_markup(reply_markup=None)


def callback_chat_id(query) -> int:
    return query.message.chat_id if query.message else 0


def callback_thread_id(query) -> Optional[int]:
    return getattr(query.message, "message_thread_id", None) if query.message else None


def callback_matches_task_thread(query, task: Task) -> bool:
    message_thread_id = callback_thread_id(query)
    return message_thread_id is None or task.thread_id == message_thread_id


def is_task_assignee(task: Task, username: Optional[str]) -> bool:
    return task.assignee != "@unassigned" and (username or "").lower() == task.assignee.lstrip("@").lower()


async def user_can_manage_task(user_id: int, task: Task, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id == task.creator_id:
        return True
    try:
        member = await context.bot.get_chat_member(task.chat_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def edit_task_control_message(query, task: Task, chat_id: int, confirm_done: bool = False) -> None:
    await query.edit_message_text(
        text=task_control_panel_text(task),
        reply_markup=task_control_keyboard(task, chat_id, confirm_done=confirm_done),
    )


async def handle_order_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    try:
        _, task_id_str, action = (query.data or "").split(":", 2)
        task_id = int(task_id_str)
    except Exception:
        await query.answer("⚠️ Invalid action.", show_alert=True)
        return

    chat_id = callback_chat_id(query)
    task = get_task(task_id, chat_id)
    if not task:
        await query.answer("❌ Task not found.", show_alert=True)
        return
    if not callback_matches_task_thread(query, task):
        await query.answer("⚠️ This button belongs to a different topic.", show_alert=True)
        return

    username = update.effective_user.username

    if action == "refresh":
        await query.answer("Updated")
        await edit_task_control_message(query, task, chat_id)
        return

    if action == "deadline":
        await query.answer(deadline_detail(task.deadline_utc), show_alert=True)
        return

    if action == "assign_menu":
        if not await user_can_manage_task(update.effective_user.id, task, context):
            await query.answer("🛡️ Only the task creator or an admin can assign this.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            text=f"Choose assignee for task #{task.id}:",
            reply_markup=task_assign_control_keyboard(task, chat_id),
        )
        return

    if action == "feedback":
        if not await user_can_manage_task(update.effective_user.id, task, context):
            await query.answer("🛡️ Only the task creator or an admin can submit feedback.", show_alert=True)
            return
        request_id = (
            f"fb-{task.id}-{update.effective_user.id}-"
            f"{int(datetime.now(tz=ZoneInfo('UTC')).timestamp())}-{random.randint(1000, 9999)}"
        )
        prompt = await query.message.reply_text(
            "📝 Send the feedback as a reply to this message. It will be saved and shown in this order topic.",
            message_thread_id=task.thread_id,
            reply_markup=feedback_cancel_keyboard(request_id),
        )
        create_pending_feedback(
            chat_id=chat_id,
            thread_id=task.thread_id,
            task_id=task.id,
            user_id=update.effective_user.id,
            prompt_message_id=prompt.message_id,
            request_id=request_id,
        )
        await query.answer("Feedback form opened")
        return

    if action == "confirm_done":
        if task.status == "done":
            await query.answer("Already completed.", show_alert=True)
            await edit_task_control_message(query, task, chat_id)
            return
        if not await user_can_manage_task(update.effective_user.id, task, context):
            await query.answer("🛡️ Only the task creator or an admin can complete this.", show_alert=True)
            return
        await query.answer("Confirm completion")
        await edit_task_control_message(query, task, chat_id, confirm_done=True)
        return

    if action == "done":
        if task.status == "done":
            await query.answer("Already completed.", show_alert=True)
            await edit_task_control_message(query, task, chat_id)
            return
        if not await user_can_manage_task(update.effective_user.id, task, context):
            await query.answer("🛡️ Only the task creator or an admin can complete this.", show_alert=True)
            return
        marked, transitioned = mark_task_done(task.id, chat_id)
        if not marked:
            await query.answer("❌ Task not found.", show_alert=True)
            return
        if not transitioned:
            await query.answer("Already completed.", show_alert=True)
            refreshed_done = get_task(task.id, chat_id) or task
            await edit_task_control_message(query, refreshed_done, chat_id)
            return
        cancel_task_jobs(context.application, task.id)
        refreshed = get_task(task.id, chat_id) or task
        await query.answer("Completed")
        await edit_task_control_message(query, refreshed, chat_id)
        await reply_completion_message(query.message, message_thread_id=task.thread_id)
        return

    if action in {"start", "blocked", "submit"}:
        if not is_task_assignee(task, username):
            await query.answer("🧑‍💻 Only the assignee can use this.", show_alert=True)
            return
        if task.status == "done":
            await query.answer("This task is already completed.", show_alert=True)
            await edit_task_control_message(query, task, chat_id)
            return

    if action == "start":
        if task.status == "in_progress":
            await query.answer("Already in progress.", show_alert=True)
            return
        if not set_task_status(task.id, chat_id, "in_progress"):
            await query.answer("This task is already completed.", show_alert=True)
            refreshed_done = get_task(task.id, chat_id) or task
            await edit_task_control_message(query, refreshed_done, chat_id)
            return
        refreshed = get_task(task.id, chat_id) or task
        await query.answer("Started")
        await edit_task_control_message(query, refreshed, chat_id)
        await query.message.reply_text("🔵 Task in progress.\nGood luck 🚀", message_thread_id=task.thread_id)
        return

    if action == "blocked":
        if task.status == "blocked":
            await query.answer("Already marked as needing details.", show_alert=True)
            return
        if not set_task_status(task.id, chat_id, "blocked"):
            await query.answer("This task is already completed.", show_alert=True)
            refreshed_done = get_task(task.id, chat_id) or task
            await edit_task_control_message(query, refreshed_done, chat_id)
            return
        refreshed = get_task(task.id, chat_id) or task
        creator_mention = f'<a href="tg://user?id={task.creator_id}">task creator</a>'
        await query.answer("Marked as needing details")
        await edit_task_control_message(query, refreshed, chat_id)
        await query.message.reply_text(
            f"🔴 Task blocked.\nWaiting for more details from {creator_mention} 🤔",
            parse_mode=ParseMode.HTML,
            message_thread_id=task.thread_id,
        )
        return

    if action == "submit":
        if task.status in {"submitted", "sent_to_client"}:
            await query.answer("Already submitted.", show_alert=True)
            await edit_task_control_message(query, task, chat_id)
            return
        if not set_task_status(task.id, chat_id, "submitted"):
            await query.answer("This task is already completed.", show_alert=True)
            refreshed_done = get_task(task.id, chat_id) or task
            await edit_task_control_message(query, refreshed_done, chat_id)
            return
        refreshed = get_task(task.id, chat_id) or task
        await query.answer("Submitted internally")
        await edit_task_control_message(query, refreshed, chat_id)
        await query.message.reply_text(
            "🟣 Task submitted in Telegram. This does not deliver anything to Fiverr.",
            reply_markup=task_delivery_keyboard(task.id),
            message_thread_id=task.thread_id,
        )
        return

    await query.answer("⚠️ Unknown action.", show_alert=True)


async def handle_order_assign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    try:
        _, task_id_str, username = (query.data or "").split(":", 2)
        task_id = int(task_id_str)
    except Exception:
        await query.answer("⚠️ Invalid assignment.", show_alert=True)
        return

    chat_id = callback_chat_id(query)
    task = get_task(task_id, chat_id)
    if not task:
        await query.answer("❌ Task not found.", show_alert=True)
        return
    if task.status == "done":
        await query.answer("This task is already completed.", show_alert=True)
        await edit_task_control_message(query, task, chat_id)
        return
    if not callback_matches_task_thread(query, task):
        await query.answer("⚠️ This button belongs to a different topic.", show_alert=True)
        return
    if not await user_can_manage_task(update.effective_user.id, task, context):
        await query.answer("🛡️ Only the task creator or an admin can assign this.", show_alert=True)
        return

    assignee = f"@{username}"
    if task.assignee == assignee:
        await query.answer(f"Already assigned to {assignee}", show_alert=True)
        await edit_task_control_message(query, task, chat_id)
        return

    set_task_assignee(task.id, chat_id, assignee)
    refreshed = get_task(task.id, chat_id)
    if not refreshed:
        await query.answer("❌ Task not found.", show_alert=True)
        return

    await query.answer(f"Assigned to {assignee}")
    await edit_task_control_message(query, refreshed, chat_id)
    await query.message.reply_text(f"✅ Assigned to {assignee}.", message_thread_id=task.thread_id)
    await send_assignee_prompt_to_chat(context.bot, chat_id, task.thread_id, refreshed)


async def on_startup(app: Application) -> None:
    mark_stale_pending_sends_uncertain()
    # Reschedule reminders for existing open tasks
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'done'"
        ).fetchall()
        for row in rows:
            task = task_from_row(row)
            await schedule_task_jobs(app, task)

    # Daily update at 10 PM Los Angeles time.
    daily_time = datetime.now(tz=TZ).replace(hour=22, minute=0, second=0, microsecond=0).timetz()
    app.job_queue.run_daily(
        daily_update_job,
        time=daily_time,
        name="daily_update_10pm",
    )
    report_time = local_schedule_time("report_time", REPORT_TIME_DEFAULT)
    app.job_queue.run_daily(
        weekly_completed_projects_job,
        time=report_time,
        name="weekly_completed_projects",
    )
    app.job_queue.run_daily(
        monthly_completed_projects_job,
        time=report_time,
        name="monthly_completed_projects",
    )
    game_time = local_schedule_time("game_invite_time", GAME_INVITE_TIME_DEFAULT)
    app.job_queue.run_daily(
        game_invite_job,
        time=game_time,
        name="game_invites",
    )
    app.job_queue.run_repeating(
        close_expired_game_sessions_job,
        interval=timedelta(minutes=1),
        first=timedelta(minutes=1),
        name="close_expired_game_sessions",
    )
    await catch_up_due_reports(SimpleNamespace(bot=app.bot))

    await start_webserver(app)


def main() -> None:
    setup_logging()
    init_db()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is not set. Put it in .env")

    application = Application.builder().token(token).build()
    if application.job_queue is None:
        raise SystemExit(
            "JobQueue is not available. Reinstall dependencies with "
            "`pip install -r requirements.txt` to enable reminders."
        )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("task", cmd_task))
    application.add_handler(CommandHandler("order", cmd_order))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("feedback", cmd_feedback))
    application.add_handler(CommandHandler("games", cmd_games))
    application.add_handler(CommandHandler("approve", cmd_approve))
    application.add_handler(CallbackQueryHandler(on_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reply_message))

    application.post_init = on_startup

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
