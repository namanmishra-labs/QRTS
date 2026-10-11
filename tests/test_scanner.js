const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class FakeElement {
  constructor() {
    this.listeners = {};
    this.classNames = new Set();
    this.disabled = false;
    this.hidden = false;
    this.checked = false;
    this.video = null;
    this.textContent = '';
    this.className = '';
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) { return selector === 'video' ? this.video : null; }
  scrollIntoView() {}
  focus() {}
  get classList() {
    return {
      add: (name) => this.classNames.add(name),
      remove: (name) => this.classNames.delete(name),
      toggle: (name, force) => force ? this.classNames.add(name) : this.classNames.delete(name),
    };
  }
}

async function runScenario({failures = [], cameras = [], makeVideo = true, fetchImpl, confirm = () => true} = {}) {
  const ids = [
    'start-scan', 'stop-scan', 'switch-camera', 'notice', 'main', 'attendance-modal', 'checkpoint-section',
    'reader', 'camera-help', 'confirm-update', 'logout', 'mark-present', 'modal-feedback', 'cancel-scan',
    'attendee-name', 'attendee-id',
  ];
  const elements = Object.fromEntries(ids.map((id) => [`#${id}`, new FakeElement()]));
  elements['#attendance-modal'].hidden = true;
  const checkpoints = ['day1_morning', 'day1_evening', 'day2_morning', 'day2_evening'].map((value) => {
    const item = new FakeElement(); item.value = value; return item;
  });
  checkpoints[0].checked = true;
  const reader = elements['#reader'];
  const calls = [];
  const instances = [];
  class MockScanner {
    constructor() { this.id = instances.length + 1; instances.push(this); }
    async start(camera, _config, _success, _error) {
      this.onScan = _success;
      calls.push({op: 'start', instance: this.id, camera});
      const failure = failures.shift();
      if (failure) throw failure;
      if (makeVideo) {
        const actualFacing = typeof camera === 'string'
          ? (cameras.find((item) => item.id === camera) || {}).facingMode || 'user'
          : camera.facingMode;
        reader.video = {
          srcObject: {getVideoTracks: () => [{
            readyState: 'live', getSettings: () => ({facingMode: actualFacing}),
          }]},
          readyState: 2,
          videoWidth: 640,
          videoHeight: 480,
          paused: true,
          async play() { this.paused = false; },
        };
      }
    }
    async stop() { calls.push({op: 'stop', instance: this.id}); }
    async clear() {
      calls.push({op: 'clear', instance: this.id});
      reader.video = null;
    }
    static async getCameras() { return cameras; }
  }
  const context = {
    document: {
      querySelector: (selector) => selector === 'meta[name="csrf-token"]'
        ? {content: 'test-csrf'} : (selector === 'main' ? elements['#main'] : elements[selector]),
      querySelectorAll: (selector) => selector === 'input[name="checkpoint"]' ? checkpoints : [],
      addEventListener() {},
    },
    window: {
      Html5Qrcode: MockScanner,
      setTimeout,
      addEventListener() {},
      confirm,
      location: {assign() {}},
    },
    navigator: {mediaDevices: {getUserMedia: async () => { throw new Error('library handles camera access'); }}},
    Html5Qrcode: MockScanner,
    fetch: fetchImpl || (async () => ({ok: true, json: async () => ({})})),
    setTimeout,
    clearTimeout,
    Date,
    Promise,
    Error,
    JSON,
  };
  vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
  return {elements, calls, instances, checkpoints};
}

function startCall(calls, index = 0) {
  return calls.filter((call) => call.op === 'start')[index];
}

(async () => {
  const normal = await runScenario();
  const starting = normal.elements['#start-scan'].listeners.click();
  assert.equal(normal.elements['#switch-camera'].disabled, true, 'switch is disabled during startup');
  await starting;
  assert.equal(startCall(normal.calls).camera.facingMode, 'environment', 'rear camera is the default');
  assert.match(normal.elements['#notice'].textContent, /Back camera ready/);

  const switchingFront = normal.elements['#switch-camera'].listeners.click();
  assert.equal(normal.elements['#switch-camera'].disabled, true, 'switch is disabled during camera change');
  const duplicateSwitch = normal.elements['#switch-camera'].listeners.click();
  await Promise.all([switchingFront, duplicateSwitch]);
  assert.equal(startCall(normal.calls, 1).camera.facingMode, 'user', 'first switch selects front');
  assert.equal(normal.elements['#switch-camera'].textContent, 'Switch to Back Camera');
  const firstStop = normal.calls.findIndex((call) => call.op === 'stop' && call.instance === 1);
  const firstClear = normal.calls.findIndex((call) => call.op === 'clear' && call.instance === 1);
  const frontStart = normal.calls.findIndex((call) => call.op === 'start' && call.instance === 2);
  assert.ok(firstStop >= 0 && firstClear > firstStop && frontStart > firstClear,
    'old rear scanner must stop and clear before front scanner starts');

  await normal.elements['#switch-camera'].listeners.click();
  assert.equal(startCall(normal.calls, 2).camera.facingMode, 'environment', 'second switch returns to rear');
  assert.equal(normal.elements['#switch-camera'].textContent, 'Switch to Front Camera');

  const attendee = {
    registration_id: 'REG-TEST', name: 'Test Attendee',
    day1_morning: 'Present', day1_evening: 'Registered',
    day2_morning: 'Registered', day2_evening: 'Present',
  };
  const sent = [];
  let resolveUpdate;
  const appFlow = await runScenario({fetchImpl: async (path, options) => {
    const body = JSON.parse(options.body);
    sent.push(body);
    if (path === '/api/lookup') return {ok: true, json: async () => ({attendee})};
    return await new Promise((resolve) => { resolveUpdate = resolve; });
  }});
  await appFlow.elements['#start-scan'].listeners.click();
  await appFlow.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(appFlow.elements['#attendee-name'].textContent, 'Test Attendee');
  assert.equal(appFlow.elements['#attendee-id'].textContent, 'REG-TEST');
  assert.equal(appFlow.elements['#attendance-modal'].hidden, false, 'valid attendee opens modal');
  assert.equal(appFlow.elements['#mark-present'].checked, false, 'checkbox defaults unchecked');
  appFlow.checkpoints.forEach((option) => { option.checked = option.value === 'day2_evening'; });
  appFlow.checkpoints[3].listeners.change();
  const submit = appFlow.elements['#confirm-update'].listeners.click;
  const firstSubmit = submit({currentTarget: appFlow.elements['#confirm-update']});
  const secondSubmit = submit({currentTarget: appFlow.elements['#confirm-update']});
  assert.equal(sent.length, 2, 'only lookup plus one attendance request despite duplicate clicks');
  assert.equal(sent[1].checkpoint, 'day2_evening');
  assert.equal(sent[1].status, 'Registered', 'unchecked checkbox maps to Registered');
  assert.equal(sent[1].expected_status, 'Present');
  assert.equal(Object.keys(sent[1]).filter((key) => key === 'checkpoint').length, 1);
  resolveUpdate({ok: true, json: async () => ({status: 'Registered', duplicate: false})});
  await Promise.all([firstSubmit, secondSubmit]);
  assert.equal(appFlow.elements['#attendance-modal'].hidden, true, 'success closes modal');
  assert.equal(appFlow.elements['#mark-present'].checked, false, 'success resets checkbox');
  assert.equal(appFlow.elements['#start-scan'].textContent, 'Scan Next Attendee');
  assert.equal(appFlow.elements['#attendee-name'].textContent, '', 'success clears attendee identity');
  assert.match(appFlow.elements['#notice'].textContent, /updated successfully/i);
  await appFlow.elements['#start-scan'].listeners.click();
  assert.equal(appFlow.instances.length, 2, 'next attendee scan can start immediately');

  let presentRequest;
  const presentFlow = await runScenario({fetchImpl: async (path, options) => {
    if (path === '/api/lookup') return {ok: true, json: async () => ({attendee})};
    presentRequest = JSON.parse(options.body);
    return {ok: true, json: async () => ({status: 'Present', duplicate: false})};
  }});
  await presentFlow.elements['#start-scan'].listeners.click();
  await presentFlow.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  presentFlow.elements['#mark-present'].checked = true;
  await presentFlow.elements['#confirm-update'].listeners.click();
  assert.equal(presentRequest.status, 'Present', 'checked checkbox maps to Present');
  assert.equal(presentFlow.elements['#attendance-modal'].hidden, true, 'checked checkbox submits Present');

  const invalidFlow = await runScenario({fetchImpl: async () => ({
    ok: false, json: async () => ({error: 'QR code was not found.', code: 'unknown_qr'}),
  })});
  await invalidFlow.elements['#start-scan'].listeners.click();
  await invalidFlow.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(invalidFlow.elements['#attendance-modal'].hidden, true, 'invalid QR does not open attendance modal');
  assert.match(invalidFlow.elements['#notice'].textContent, /QR code was not found/);

  const cancelFlow = await runScenario({fetchImpl: async () => ({ok: true, json: async () => ({attendee})})});
  await cancelFlow.elements['#start-scan'].listeners.click();
  await cancelFlow.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  cancelFlow.elements['#mark-present'].checked = true;
  await cancelFlow.elements['#cancel-scan'].listeners.click();
  assert.equal(cancelFlow.elements['#attendance-modal'].hidden, true, 'cancel closes modal');
  assert.equal(cancelFlow.elements['#mark-present'].checked, false, 'cancel resets checkbox');
  assert.equal(cancelFlow.instances.length, 2, 'cancel resumes scanner without page reload');

  const failedFlow = await runScenario({fetchImpl: async (path) => {
    if (path === '/api/lookup') return {ok: true, json: async () => ({attendee})};
    return {ok: false, json: async () => ({error: 'Update failed safely.', code: 'sheets_error'})};
  }});
  await failedFlow.elements['#start-scan'].listeners.click();
  await failedFlow.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  failedFlow.checkpoints.forEach((option) => { option.checked = option.value === 'day1_evening'; });
  failedFlow.checkpoints[1].listeners.change();
  failedFlow.elements['#mark-present'].checked = true;
  await failedFlow.elements['#confirm-update'].listeners.click();
  assert.equal(failedFlow.elements['#attendance-modal'].hidden, false, 'failed request keeps modal available for retry');
  assert.match(failedFlow.elements['#modal-feedback'].textContent, /Update failed safely/);
  assert.equal(failedFlow.elements['#modal-feedback'].className, 'notice error');
  assert.equal(failedFlow.elements['#mark-present'].checked, true, 'failed request preserves checkbox');

  const unavailable = await runScenario({
    failures: [Object.assign(new Error('unavailable'), {name: 'NotFoundError'})],
  });
  await unavailable.elements['#start-scan'].listeners.click();
  assert.match(unavailable.elements['#notice'].textContent, /rear camera could not start/i);
  assert.equal(unavailable.elements['#notice'].className, 'notice error');
  assert.equal(unavailable.elements['#start-scan'].disabled, false);
  assert.equal(unavailable.elements['#switch-camera'].disabled, false);
  assert.ok(unavailable.calls.some((call) => call.op === 'clear'), 'failed scanner is cleaned up');
  assert.doesNotMatch(unavailable.elements['#notice'].textContent, /ready/i);

  const laptop = await runScenario({
    failures: [Object.assign(new Error('rear missing'), {name: 'NotFoundError'})],
    cameras: [{id: 'available-camera', label: 'Integrated Camera', facingMode: 'user'}],
  });
  await laptop.elements['#start-scan'].listeners.click();
  assert.equal(startCall(laptop.calls, 1).camera, 'available-camera', 'fallback selects an available camera');
  assert.match(laptop.elements['#notice'].textContent, /available camera is running/i);
  assert.equal(laptop.elements['#switch-camera'].textContent, 'Switch to Back Camera');
  console.log('scanner camera switching, QR modal and attendance checks passed');
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
