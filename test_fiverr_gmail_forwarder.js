// Offline Google Apps Script regression suite. Run: node test_fiverr_gmail_forwarder.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'fiverr_gmail_forwarder.gs'), 'utf8');

function message(id, fields = {}) {
  const data = {
    from: 'Fiverr <noreply@e.fiverr.com>', date: new Date(),
    subject: "Great news: You've received a milestones order from arkeo7",
    body: 'A new milestones order.',
    html: '<a href="https://www.fiverr.com/users/example/orders/FO12345678">milestones_gig_order_started_to_seller</a>',
    ...fields,
  };
  return { getId: () => id, getFrom: () => data.from, getDate: () => data.date,
    getSubject: () => data.subject, getPlainBody: () => data.body, getBody: () => data.html };
}
function thread(id, messages, labeled = false) {
  return { labeled, getId: () => id, getMessages: () => messages,
    addLabel() { this.labeled = true; }, removeLabel() { this.labeled = false; } };
}
function harness(threads, responses = [], options = {}) {
  const state = { calls: [], searches: [], properties: {}, released: 0 };
  const context = {
    Date: options.clock || Date,
    GmailApp: {
      getUserLabelByName: () => ({}), createLabel: () => ({}),
      search(query, start, size) {
        state.searches.push({ query, start, size });
        if (options.searchError) throw new Error('search failed');
        return threads.slice(start, start + size);
      },
    },
    PropertiesService: { getScriptProperties: () => ({
      getProperty: key => state.properties[key],
      setProperty: (key, value) => { state.properties[key] = value; },
    }) },
    LockService: { getScriptLock: () => ({ tryLock: () => !options.locked,
      releaseLock: () => { state.released++; } }) },
    UrlFetchApp: { fetch(url, config) {
      state.calls.push({ url, config, payload: JSON.parse(config.payload) });
      const response = (options.fetchResponse && options.fetchResponse(JSON.parse(config.payload))) || responses.shift() || { code: 200, body: 'ok' };
      if (response instanceof Error) throw response;
      return { getResponseCode: () => response.code, getContentText: () => response.body };
    } },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  state.run = () => context.forwardFiverrOrders();
  return state;
}
const cases = {
  'milestones payload includes HTML, identifiers, sender, and date'() {
    const msg = message('milestone');
    const h = harness([thread('t', [msg])]); h.run();
    assert.deepEqual(h.calls[0].payload, { messageId: 'milestone', threadId: 't',
      date: msg.getDate().toISOString(), from: msg.getFrom(), subject: msg.getSubject(),
      body: msg.getPlainBody(), htmlBody: msg.getBody() });
    assert.equal(h.properties.processed_milestone, '1');
    assert.equal(h.calls[0].config.followRedirects, false);
    assert(!h.searches[0].query.includes('-label:'));
    assert(!h.searches[0].query.includes('Great news'));
    assert(!h.searches[0].query.includes('FO'));
  },
  'requirements appended to a labeled thread are forwarded once'() {
    const messages = [message('order')]; const t = thread('t', messages);
    const h = harness([t]); h.run();
    assert(t.labeled);
    messages.push(message('requirements', { subject: 'Requirements received', body: 'Ready to start' }));
    h.run(); h.run();
    assert.deepEqual(h.calls.map(c => c.payload.messageId), ['order', 'requirements']);
  },
  'failed HTTP, network, redirect, and invalid acknowledgment stay retryable'() {
    for (const failure of [{ code: 500, body: 'failed' }, { code: 403, body: 'invalid token' },
      { code: 302, body: 'redirect' }, { code: 200, body: '<html>not the bot</html>' },
      new Error('network failure')]) {
      const t = thread('t', [message('retry'), message('good')], true);
      const h = harness([t], [failure]); h.run();
      assert.equal(h.properties.processed_retry, undefined);
      assert.equal(h.properties.processed_good, '1');
      assert(!t.labeled);
      h.run(); assert(t.labeled);
      assert.equal(h.properties.processed_retry, '1');
      assert.deepEqual(h.calls.map(c => c.payload.messageId), ['retry', 'good', 'retry']);
    }
  },
  'thread neighbors from other senders and old messages are not transmitted'() {
    const h = harness([thread('t', [
      message('valid'), message('user', { from: 'owner@example.com' }),
      message('spoof', { from: 'noreply@fiverr.com <attacker@example.com>' }),
      message('suffix', { from: 'noreply@fiverr.com.evil.example' }),
      message('old', { date: new Date(Date.now() - 3 * 86400000) }),
      message('second-domain', { from: 'NOREPLY@FIVERR.COM' }),
    ])]); h.run();
    assert.deepEqual(h.calls.map(c => c.payload.messageId), ['valid', 'second-domain']);
  },
  'search pagination reaches messages beyond the first 100 threads'() {
    const h = harness(Array.from({ length: 205 }, (_, i) => thread('t' + i, [message('m' + i)])));
    h.run();
    assert.equal(h.calls.length, 205);
    assert.deepEqual(h.searches.map(s => s.start), [0, 100, 200]);
  },
  'execution budget stops safely and next trigger resumes by message ID'() {
    const now = Date.now(); let ticks = 0;
    const clock = { now: () => now + (++ticks >= 4 ? 240000 : 0) };
    const t = thread('t', [message('first'), message('later')], true);
    const h = harness([t], [], { clock }); h.run();
    assert.deepEqual(h.calls.map(c => c.payload.messageId), ['first']);
    assert(!t.labeled); assert.equal(h.released, 1);
    clock.now = () => now;
    h.run();
    assert.deepEqual(h.calls.map(c => c.payload.messageId), ['first', 'later']);
    assert(t.labeled); assert.equal(h.released, 2);
  },
  'unrelated Fiverr inbox and marketing messages stay local'() {
    const h = harness([thread('t', [
      message('inbox', { subject: 'You have a new message', body: 'Private buyer chat', html: '<p>Private chat</p>' }),
      message('marketing', { subject: 'Grow your business', body: 'Tips for sellers FO', html: '' }),
    ])]); h.run();
    assert.equal(h.calls.length, 0);
    assert.deepEqual(Object.keys(h.properties).filter(key => key.startsWith('processed_')), []);
  },
  'known orders without IDs still reach server quarantine'() {
    const h = harness([thread('t', [
      message('phrase', { body: 'No order ID present', html: '' }),
      message('template', { subject: 'Update', body: '', html: '<a href="https://example.test/?email_name=milestones_gig_order_started_to_seller">Open</a>' }),
      message('requirements-template', { subject: 'Update', body: '', html: 'email_name=work_plan_order_requirements_received' }),
      message('requirements-phrase', { subject: 'Update', body: 'Buyer has sent the requirements and your order is now in progress', html: '' }),
    ])]); h.run();
    assert.equal(h.calls.length, 4);
  },
  'valid order ID alone in HTML is a candidate'() {
    const h = harness([thread('t', [message('html-id', {
      subject: 'Order update', body: '', html: '<a href="https://example.test/orders/FO12345678">View</a>',
    })])]); h.run();
    assert.equal(h.calls.length, 1);
  },
  'persistent slow failures cannot starve later messages or new arrivals'() {
    let now = Date.now();
    const threads = [thread('slow-thread', [message('slow'), message('same-thread-later')]),
      thread('later-thread', [message('later')])];
    const h = harness(threads, [], {
      clock: { now: () => now },
      fetchResponse(payload) {
        if (payload.messageId === 'slow') {
          now += 240000;
          return { code: 503, body: 'retry' };
        }
      },
    });
    h.run();
    assert.deepEqual(h.calls.map(c => c.payload.messageId), ['slow']);
    threads.unshift(thread('arrived-mid-sweep', [message('mid-sweep')]));
    h.run();
    assert.deepEqual(h.calls.map(c => c.payload.messageId), ['slow', 'same-thread-later', 'later']);
    assert(!threads.find(t => t.getId() === 'slow-thread').labeled);
    threads.unshift(thread('new-thread', [message('new')]));
    h.run(); h.run();
    assert(h.calls.some(c => c.payload.messageId === 'new'));
    assert(h.calls.some(c => c.payload.messageId === 'mid-sweep'));
    assert.equal(h.calls.filter(c => c.payload.messageId === 'slow').length, 2);
    assert.equal(h.calls.filter(c => c.payload.messageId === 'later').length, 1);
  },
  'overlapping trigger exits without forwarding'() {
    const h = harness([thread('t', [message('m')])], [], { locked: true }); h.run();
    assert.equal(h.calls.length, 0); assert.equal(h.released, 0);
  },
  'lock releases after search throws'() {
    const h = harness([], [], { searchError: true });
    assert.throws(h.run, /search failed/); assert.equal(h.released, 1);
  },
};
for (const [name, test] of Object.entries(cases)) { test(); console.log('PASS ' + name); }
console.log(`${Object.keys(cases).length} tests passed (offline VM mocks only).`);
