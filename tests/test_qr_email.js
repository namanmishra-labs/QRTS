const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(id = '') {
    this.id = id;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.textContent = '';
    this.className = '';
    this.attributes = {};
    this.type = '';
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll(selector) {
    if (selector === 'input[data-registration-id]') return this.children
      .flatMap((row) => row.children.flatMap((cell) => cell.children))
      .filter((child) => child.dataset.registrationId);
    if (selector === 'input[data-registration-id]:checked') return this.querySelectorAll('input[data-registration-id]')
      .filter((input) => input.checked);
    return [];
  }
}

async function scenario(candidateRows) {
  const ids = ['.qr-email-management', '#candidate-rows', '#message', '#select-all',
    '#send-all', '#send-selected', '#retry-failed', '#reload'];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
  elements['.qr-email-management'].dataset.testMode = 'false';
  const candidates = candidateRows || [
    {registration_id: 'REG-1', name: 'One', email: '***@example.test', email_valid: true, email_status: 'Pending'},
    {registration_id: 'REG-2', name: 'Two', email: '***@example.test', email_valid: true, email_status: 'Sent'},
    {registration_id: 'REG-3', name: 'Three', email: '***@example.test', email_valid: true, email_status: 'Failed'},
    {registration_id: 'REG-4', name: 'Four', email: 'Invalid or missing', email_valid: false, email_status: 'Pending'},
  ];
  const requests = [];
  const confirmations = [];
  const context = {
    document: {
      querySelector: (selector) => selector === 'meta[name="csrf-token"]'
        ? {content: 'csrf-test'} : elements[selector],
      getElementById: (id) => elements[`#${id}`],
      createElement: () => new Element(),
    },
    window: {confirm: (text) => { confirmations.push(text); return true; }},
    fetch: async (path, options) => {
      const body = JSON.parse(options.body);
      requests.push({path, body});
      if (path === '/api/qr-email/candidates') {
        return {ok: true, json: async () => ({candidates})};
      }
      return {ok: true, json: async () => ({selected_count: body.registration_ids.length,
        sent_count: body.registration_ids.length, failed_count: 0, skipped_count: 0,
        manual_review_count: 0})};
    },
    JSON,
  };
  vm.runInNewContext(fs.readFileSync('static/qr_email_test.js', 'utf8'), context);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return {elements, requests, confirmations};
}

(async () => {
  const all = await scenario();
  await all.elements['#send-all'].listeners.click();
  const batchCalls = all.requests.filter((request) => request.path === '/api/qr-email/batch');
  assert.equal(batchCalls.length, 1);
  assert.deepEqual(batchCalls[0].body.registration_ids, ['REG-1', 'REG-3', 'REG-4'],
    'Send to All includes pending and failed rows and lets the server report invalid email as skipped');
  assert.match(all.confirmations[0], /3 recipient/);
  assert.ok(!JSON.stringify(batchCalls[0].body).includes('example.test'),
    'request body contains no email addresses');
  assert.equal(all.elements['#message'].hidden, true, 'status refresh succeeds after batch');

  const selected = await scenario();
  const checkbox = selected.elements['#candidate-rows'].children[0].children[0].children[0];
  checkbox.checked = true;
  await checkbox.listeners.change();
  assert.equal(selected.elements['#send-selected'].disabled, false);
  await selected.elements['#send-selected'].listeners.click();
  const selectedRequest = selected.requests.find((request) => request.path === '/api/qr-email/batch');
  assert.deepEqual(selectedRequest.body.registration_ids, ['REG-1']);
  assert.equal(selectedRequest.body.action, 'selected');

  const retry = await scenario();
  await retry.elements['#retry-failed'].listeners.click();
  const retryRequest = retry.requests.find((request) => request.path === '/api/qr-email/batch');
  assert.deepEqual(retryRequest.body.registration_ids, ['REG-3'], 'Retry Failed targets only failed rows');
  assert.ok(retryRequest.body.confirm_send);
  const many = Array.from({length: 8}, (_, index) => ({
    registration_id: `BULK-${index + 1}`, name: `Attendee ${index + 1}`,
    email: '***@example.test', email_valid: true, email_status: 'Pending',
  }));
  const chunked = await scenario(many);
  await chunked.elements['#send-all'].listeners.click();
  const chunks = chunked.requests.filter((request) => request.path === '/api/qr-email/batch');
  assert.deepEqual(chunks.map((request) => request.body.registration_ids.length), [3, 3, 2],
    'bulk sends use bounded groups of three');
  console.log('QR email management selection and batch controls passed');
})().catch((error) => {
  console.error(error.stack);
  process.exitCode = 1;
});
