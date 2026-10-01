const WEBHOOK_URL = "https://telegram-task-bot-1-3pot.onrender.com/gmail/inbound";
const TOKEN = "REPLACE_WITH_RENDER_GMAIL_WEBHOOK_TOKEN";
const LABEL_NAME = "FiverrProcessed";

function forwardFiverrOrders() {
  const label = getOrCreateLabel_(LABEL_NAME);
  const props = PropertiesService.getScriptProperties();
  const query = [
    "from:(noreply@e.fiverr.com OR noreply@fiverr.com)",
    "newer_than:2d",
    "-label:" + LABEL_NAME,
    `("Great news: You've received an order from" OR "You just received an order from" OR "FO")`,
  ].join(" ");
  const threads = GmailApp.search(query, 0, 20);

  for (const thread of threads) {
    let threadComplete = true;

    for (const msg of thread.getMessages()) {
      const messageId = msg.getId();
      const processedKey = "processed_" + messageId;
      if (props.getProperty(processedKey)) continue;

      const payload = {
        messageId,
        threadId: thread.getId(),
        date: msg.getDate().toISOString(),
        from: msg.getFrom() || "",
        subject: msg.getSubject() || "",
        body: msg.getPlainBody() || "",
        htmlBody: msg.getBody() || "",
      };

      const res = UrlFetchApp.fetch(WEBHOOK_URL, {
        method: "post",
        contentType: "application/json",
        payload: JSON.stringify(payload),
        headers: { "X-Webhook-Token": TOKEN },
        muteHttpExceptions: true,
      });

      if (res.getResponseCode() === 200) {
        props.setProperty(processedKey, "1");
      } else {
        threadComplete = false;
      }
    }

    if (threadComplete) {
      thread.addLabel(label);
    }
  }
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}
