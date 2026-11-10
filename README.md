# Telegram Task Bot

A lightweight Telegram group bot for task creation, assignment, and deadline reminders.

## Features
- Create tasks with deadlines and assignees
- List and view tasks
- Mark tasks done
- Automatic reminders 24h and 1h before deadline
- Admin-only task creation and closure

## Setup
1. Create a Telegram bot with BotFather and get the token.
2. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
3. Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Run the bot:

```bash
python main.py
```

5. Add the bot to your group and make it an admin (required to read admin status).

## Mailgun Inbound (Optional)
- Set these in `.env`:
  - `MAILGUN_SIGNING_KEY` (from Mailgun)
  - `MAILGUN_ALLOWED_SENDER` (default `fiverr.com`)
  - `PORT` (default `8000`)
- Run in your group:
  - `/task setgeneral` in the main chat or General topic
  - `/task setowner` (admin only)
  - `/task setassignee @user` (admin only)

## Gmail Webhook (Optional)
Use a Google Apps Script to forward Fiverr emails to the bot.
- Set `GMAIL_WEBHOOK_TOKEN` in `.env` / Render env.
- Endpoint: `POST /gmail/inbound` with JSON `{ "subject": "...", "body": "..." }`
- Update the existing Apps Script with `fiverr_gmail_forwarder.gs` separately from
  the Render deployment. Preserve its existing webhook URL/token, trigger, and
  script properties; do not copy the repository's placeholder token into production.
- The forwarder scans the last two days and sends only order-related messages from
  the two configured Fiverr notification addresses. Message IDs, not Gmail thread
  labels, prevent duplicate delivery. Later requirements messages in labeled
  threads are still checked. A saved scan cursor keeps slow failures from blocking
  later messages. This is not a historical backfill.
- HTTP 200 with body `ok` acknowledges a handled email (created, ignored, deferred,
  or quarantined). Transport failures, other responses, and missing bot routing or
  owner settings remain retryable.
- Ordinary and milestones order templates are supported. A deadline must contain
  an explicit full calendar date, such as `Due Nov 20, 2026` or
  `Delivery date: 2026-11-20`. Incomplete, unsupported, or conflicting deadlines
  are quarantined instead of guessed. A later requirements email can use a stored
  pending deadline only when it contains no new deadline evidence.
- A recognized milestone order with an order ID and genuinely missing deadline
  creates one topic in `awaiting_due` state, without a task, reminders, or intro
  messages. Replay and restart reuse that topic. A later explicit valid deadline
  upgrades it to a task in the same topic; task creation and order linkage commit
  atomically. Invalid or conflicting deadlines remain quarantined.
  Verify existing Telegram/database records before recovering historical orders.
  Do not replay an uncertain topic-creation attempt blindly.

## Tests
Run the offline suites after installing requirements (Node.js is also needed):

```bash
python -m unittest -q
node test_fiverr_gmail_forwarder.js
python -m compileall -q main.py test_fiverr_intake.py
```

The suites use temporary databases and fake Telegram/Gmail/webhook clients.
They do not verify the deployed Apps Script trigger or make live API calls.
Application log formatting redacts configured credentials and Telegram URLs;
routine HTTP client INFO logging is disabled. Existing historical logs are not
rewritten, and credentials are not rotated by this code change.

## Commands
- `/task add "Title" @user YYYY-MM-DD HH:MM`
- `/task list`
- `/task view <id>`
- `/task done <id>`
- `/task submit <id>`
- `/task status`
- `/task sendissues`
- `/task sendresolve <key> delivered <message_id>`
- `/task sendresolve <key> notdelivered`
- `/feedback <feedback_id>`
- `/approve`

## Notes
- Deadlines are interpreted in the timezone set by `BOT_TIMEZONE` (default: `America/Los_Angeles`).
- Reminders are sent 24 hours and 1 hour before the deadline.
- Scheduled reports and game invites are deduped. Failed sends can retry; uncertain sends require admin reconciliation with `/task sendresolve`.
- Feedback is stored before display. If a long feedback display only partially sends, an authorized task owner/admin can redisplay it in the order topic with `/feedback <feedback_id>`.
- Weekly/monthly completion reports count tracked completion timestamps only. Periods wholly before tracking began are skipped, and partial periods call out unknown earlier coverage.
- Tasks are stored in `tasks.db` (SQLite) for persistence.
