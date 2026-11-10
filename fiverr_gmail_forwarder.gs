const WEBHOOK_URL = "https://telegram-task-bot-1-3pot.onrender.com/gmail/inbound";
const TOKEN = "REPLACE_WITH_RENDER_GMAIL_WEBHOOK_TOKEN";
const LABEL_NAME = "FiverrProcessed";
const LOOKBACK_MS = 2 * 24 * 60 * 60 * 1000;
const SEARCH_PAGE_SIZE = 100;
const MAX_RUN_MS = 4 * 60 * 1000;
const CURSOR_KEY = "fiverr_scan_cursor";

function forwardFiverrOrders() {
  // Prevent overlapping triggers from forwarding the same message concurrently.
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const label = getOrCreateLabel_(LABEL_NAME);
    const props = PropertiesService.getScriptProperties();
    const startedAt = Date.now();
    const cutoff = startedAt - LOOKBACK_MS;
    // Gmail labels and search results are thread-scoped. A processed thread can
    // receive new requirements or milestone emails, so labels are display only.
    // Search broadly, then keep unrelated inbox/marketing messages local.
    // The server remains responsible for final classification and deadlines.
    const query = "from:(noreply@e.fiverr.com OR noreply@fiverr.com) newer_than:2d";
    const cursor = readScanCursor_(props);
    for (let start = cursor.offset; ; start += SEARCH_PAGE_SIZE) {
      // Rotate through the recent window even when early messages keep failing.
      // A completed sweep wraps to zero, covering new mail and retrying failures.
      if (Date.now() - startedAt >= MAX_RUN_MS) {
        props.setProperty(CURSOR_KEY, JSON.stringify({ offset: start }));
        return;
      }
      const threads = GmailApp.search(query, start, SEARCH_PAGE_SIZE);
      const checkpointIndex = start === cursor.offset && cursor.threadId
        ? threads.findIndex(thread => thread.getId() === cursor.threadId) : -1;
      for (let index = Math.max(0, checkpointIndex); index < threads.length; index++) {
        const thread = threads[index];
        const messages = thread.getMessages();
        // Gmail results can move as new mail arrives. Never apply a message
        // cursor to a different thread; a later full sweep covers shifted mail.
        const resuming = start === cursor.offset && thread.getId() === cursor.threadId;
        let messageStart = resuming ? messages.findIndex(msg => msg.getId() === cursor.messageId) : 0;
        if (messageStart < 0) messageStart = 0;
        let threadComplete = resuming && messageStart > 0 ? cursor.threadComplete !== false : true;
        for (let messageIndex = messageStart; messageIndex < messages.length; messageIndex++) {
          const msg = messages[messageIndex];
          if (Date.now() - startedAt >= MAX_RUN_MS) {
            props.setProperty(CURSOR_KEY, JSON.stringify({
              offset: start + index, threadId: thread.getId(),
              messageId: msg.getId(), threadComplete,
            }));
            thread.removeLabel(label);
            return;
          }
          // Search matches threads, not every message within those threads.
          if (!isFiverrSender_(msg.getFrom()) || msg.getDate().getTime() < cutoff) continue;
          const messageId = msg.getId();
          const processedKey = "processed_" + messageId;
          if (props.getProperty(processedKey)) continue;

          try {
            const payload = {
              messageId,
              threadId: thread.getId(),
              date: msg.getDate().toISOString(),
              from: msg.getFrom() || "",
              subject: msg.getSubject() || "",
              body: msg.getPlainBody() || "",
              htmlBody: msg.getBody() || "",
            };
            if (!isOrderCandidate_(payload)) continue;
            const res = UrlFetchApp.fetch(WEBHOOK_URL, {
              method: "post",
              contentType: "application/json",
              payload: JSON.stringify(payload),
              headers: { "X-Webhook-Token": TOKEN },
              muteHttpExceptions: true,
              followRedirects: false,
            });
            // A generic HTTP 200 (e.g. an intermediary's HTML page) is not an
            // acknowledgment. The webhook returns 'ok' for durable outcomes.
            if (res.getResponseCode() === 200 && res.getContentText().trim() === "ok") {
              props.setProperty(processedKey, "1");
            } else {
              threadComplete = false;
            }
          } catch (error) {
            // Leave the message unprocessed for the next trigger. Do not log
            // exceptions: they may contain credentials, URLs, or email content.
            threadComplete = false;
          }
        }
        if (threadComplete) {
          thread.addLabel(label);
        } else {
          thread.removeLabel(label);
        }
      }
      if (threads.length < SEARCH_PAGE_SIZE) {
        props.setProperty(CURSOR_KEY, JSON.stringify({ offset: 0 }));
        break;
      }
    }
  } finally {
    lock.releaseLock();
  }
}

function readScanCursor_(props) {
  try {
    const cursor = JSON.parse(props.getProperty(CURSOR_KEY) || "{}");
    if (cursor && Number.isInteger(cursor.offset) && cursor.offset >= 0) return cursor;
  } catch (error) {
    // A malformed checkpoint is harmless: per-message IDs allow a fresh sweep.
  }
  return { offset: 0 };
}

function isOrderCandidate_(payload) {
  const text = [payload.subject, payload.body, payload.htmlBody].join("\n");
  // Include order IDs even in HTML links, but not standalone "FO" or words.
  if (/\bFO[A-Z0-9]{8,}\b/.test(text)) return true;
  const normalized = text.toLowerCase()
    .replace(/(?:&#0*39;|&#x0*27;|&apos;|&rsquo;|’)/g, "'")
    .replace(/\s+/g, " ");
  if (/email_name(?:=|%3d)(?:gig_order_started_seller|gig_order_with_requirements_started_to_seller|milestones_gig_order_started_to_seller|work_plan_order_requirements_received)\b/.test(normalized)) return true;
  if (/\byou(?:'ve)? (?:just )?received (?:an order|a milestones? order) from\b/.test(normalized)) return true;
  return normalized.includes("has sent the requirements and your order") &&
    normalized.includes("is now in progress");
}

function isFiverrSender_(sender) {
  const value = (sender || "").trim();
  const address = value.match(/<([^<>]+)>\s*$/);
  return /^(?:noreply@e\.fiverr\.com|noreply@fiverr\.com)$/i.test(
    (address ? address[1] : value).trim()
  );
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}
