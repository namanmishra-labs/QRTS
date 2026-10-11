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
    this.textContent = '';
    this.className = '';
    this.inert = false;
    this.video = null;
    this.attributes = {};
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector(selector) { return selector === 'video' ? this.video : null; }
  focus() {}
  get classList() {
    return {
      add: (name) => this.classNames.add(name),
      remove: (name) => this.classNames.delete(name),
    };
  }
}

async function runScenario({failures = [], cameras = [], fetchImpl} = {}) {
  const ids = [
    'main', 'start-scan', 'stop-scan', 'switch-camera', 'reader', 'notice',
    'attendance-modal', 'attendee-name', 'attendee-id', 'modal-feedback', 'toast',
    'confirm-update', 'cancel-scan',
  ];
  const elements = Object.fromEntries(ids.map((id) => [`#${id}`, new FakeElement()]));
  elements['#attendance-modal'].hidden = true;
  elements['#modal-feedback'].hidden = true;
  const checkpointOptions = [
    'day1_morning', 'day1_evening', 'day2_morning', 'day2_evening',
  ].map((value) => {
    const option = new FakeElement();
    option.value = value;
    return option;
  });
  const reader = elements['#reader'];
  const calls = [];
  const instances = [];
  const timers = [];
  const setTimer = (callback, delay) => {
    const timer = {callback, delay, cancelled: false};
    timers.push(timer);
    return timer;
  };
  const clearTimer = (timer) => { if (timer) timer.cancelled = true; };

  class MockScanner {
    constructor() { this.id = instances.length + 1; instances.push(this); }
    async start(camera, _config, success, _error) {
      this.onScan = success;
      calls.push({op: 'start', instance: this.id, camera});
      const failure = failures.shift();
      if (failure) throw failure;
      const facing = typeof camera === 'string'
        ? (cameras.find((entry) => entry.id === camera) || {}).facingMode || 'user'
        : camera.facingMode;
      reader.video = {
        srcObject: {getVideoTracks: () => [{
          readyState: 'live', getSettings: () => ({facingMode: facing}),
        }]},
        readyState: 2, videoWidth: 640, videoHeight: 480, paused: true,
        async play() { this.paused = false; },
      };
    }
    async stop() { calls.push({op: 'stop', instance: this.id}); }
    async clear() { calls.push({op: 'clear', instance: this.id}); reader.video = null; }
    static async getCameras() { return cameras; }
  }

  const context = {
    document: {
      querySelector: (selector) => selector === 'meta[name="csrf-token"]'
        ? {content: 'mock-csrf'} : (selector === 'main' ? elements['#main'] : elements[selector]),
      querySelectorAll: (selector) => selector === 'input[name="attendance-checkpoint"]'
        ? checkpointOptions : [],
      addEventListener() {},
    },
    window: {Html5Qrcode: MockScanner, setTimeout: setTimer, clearTimeout: clearTimer,
      addEventListener() {}, location: {assign() {}}},
    navigator: {mediaDevices: {getUserMedia: async () => { throw new Error('scanner library owns camera access'); }}},
    Html5Qrcode: MockScanner,
    fetch: fetchImpl || (async () => ({ok: true, json: async () => ({})})),
    setTimeout: setTimer, clearTimeout: clearTimer, Date, Promise, Error, JSON,
  };
  vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
  return {elements, checkpointOptions, calls, instances, timers};
}

function startCall(calls, index = 0) {
  return calls.filter((call) => call.op === 'start')[index];
}

(async () => {
  const cameras = await runScenario();
  assert.equal(cameras.elements['#switch-camera'].hidden, true, 'camera switch hidden before scanner starts');
  assert.equal(cameras.elements['#stop-scan'].disabled, true, 'stop disabled before scanner starts');
  await cameras.elements['#start-scan'].listeners.click();
  assert.equal(startCall(cameras.calls).camera.facingMode, 'environment', 'rear camera is default');
  assert.equal(cameras.elements['#switch-camera'].hidden, false, 'switch appears after live preview succeeds');
  assert.equal(cameras.elements['#switch-camera'].textContent, 'Change Camera');
  assert.equal(cameras.elements['#stop-scan'].disabled, false);

  await cameras.elements['#switch-camera'].listeners.click();
  assert.equal(startCall(cameras.calls, 1).camera.facingMode, 'user', 'camera switch selects front');
  const oldStop = cameras.calls.findIndex((entry) => entry.op === 'stop' && entry.instance === 1);
  const oldClear = cameras.calls.findIndex((entry) => entry.op === 'clear' && entry.instance === 1);
  const frontStart = cameras.calls.findIndex((entry) => entry.op === 'start' && entry.instance === 2);
  assert.ok(oldStop >= 0 && oldClear > oldStop && frontStart > oldClear, 'old stream is cleaned before switching');
  await cameras.elements['#switch-camera'].listeners.click();
  assert.equal(startCall(cameras.calls, 2).camera.facingMode, 'environment', 'switch returns to rear');
  await cameras.elements['#stop-scan'].listeners.click();
  assert.equal(cameras.elements['#switch-camera'].hidden, true, 'camera switch hidden after stopping');

  const attendee = {
    registration_id: 'REG-TEST', name: 'Test Attendee',
    day1_morning: 'Registered', day1_evening: 'Present',
    day2_morning: 'Registered', day2_evening: 'Present',
  };
  const requests = [];
  let resolveBatch;
  let batchAttempts = 0;
  const flow = await runScenario({fetchImpl: async (path, options) => {
    const body = JSON.parse(options.body);
    requests.push({path, body});
    if (path === '/api/lookup') return {ok: true, json: async () => ({attendee})};
    batchAttempts += 1;
    if (batchAttempts === 1) return await new Promise((resolve) => { resolveBatch = resolve; });
    return {ok: true, json: async () => ({success: true, updated_checkpoints: ['day2_evening'], already_present: ['day1_morning']})};
  }});
  await flow.elements['#start-scan'].listeners.click();
  await flow.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(flow.elements['#attendance-modal'].hidden, false, 'valid QR opens modal');
  assert.equal(flow.elements['#attendee-name'].textContent, 'Test Attendee');
  assert.equal(flow.elements['#attendee-id'].textContent, 'REG-TEST');
  assert.ok(flow.checkpointOptions.every((option) => !option.checked), 'all checkpoint boxes start unchecked');
  assert.equal(flow.elements['#switch-camera'].hidden, true, 'switch hidden during verified attendee processing');

  await flow.elements['#confirm-update'].listeners.click();
  assert.equal(requests.length, 1, 'zero selected checkpoints prevent request');
  assert.match(flow.elements['#modal-feedback'].textContent, /at least one checkpoint/i);

  flow.checkpointOptions[0].checked = true;
  flow.checkpointOptions[3].checked = true;
  const submit = flow.elements['#confirm-update'].listeners.click;
  const firstAttempt = submit();
  const duplicateAttempt = submit();
  assert.equal(requests.length, 2, 'only lookup plus one batch request despite duplicate submit');
  assert.deepEqual(requests[1].body.checkpoints, [
    {checkpoint: 'day1_morning', expected_status: 'Registered'},
    {checkpoint: 'day2_evening', expected_status: 'Present'},
  ], 'only checked checkpoints are submitted with stale-status values');
  assert.ok(requests[1].body.checkpoints.every((entry) => !('status' in entry) || entry.status === 'Present'));
  resolveBatch({ok: false, json: async () => ({
    success: false, partial: true, code: 'conflict',
    updated_checkpoints: ['day1_morning'], already_present: [], failed_checkpoint: 'day2_evening',
    current_status: 'Registered',
    error: 'Attendance changed since it was loaded.',
  })});
  await Promise.all([firstAttempt, duplicateAttempt]);
  assert.equal(flow.elements['#attendance-modal'].hidden, false, 'partial failure keeps modal open');
  assert.ok(flow.checkpointOptions[0].checked && flow.checkpointOptions[3].checked, 'failure preserves selections');
  assert.match(flow.elements['#modal-feedback'].textContent, /Partial update/);
  assert.match(flow.elements['#modal-feedback'].textContent, /Day 1 — Morning/);

  await flow.elements['#confirm-update'].listeners.click();
  assert.equal(requests.length, 3, 'retry is allowed after partial failure');
  assert.equal(requests[2].body.checkpoints.length, 2);
  assert.equal(requests[2].body.checkpoints[1].expected_status, 'Registered',
    'retry uses the server-confirmed current status after a conflict');
  assert.equal(flow.elements['#attendance-modal'].hidden, true, 'successful retry closes modal');
  assert.ok(flow.checkpointOptions.every((option) => !option.checked), 'success clears all selections');
  assert.equal(flow.elements['#attendee-name'].textContent, '', 'success clears attendee identity');
  assert.match(flow.elements['#notice'].textContent, /Attendance marked Present/);
  assert.equal(flow.elements['#toast'].textContent, 'Attendance marked Present for 2 checkpoints');
  assert.equal(flow.elements['#toast'].className, 'toast success');
  assert.equal(flow.elements['#toast'].attributes['aria-live'], 'polite');
  assert.equal(flow.timers.at(-1).delay, 3000, 'success toast dismisses after three seconds');
  flow.timers.at(-1).callback();
  assert.equal(flow.elements['#toast'].hidden, true, 'success toast auto-dismisses');
  assert.equal(flow.elements['#start-scan'].disabled, false, 'scanner can be restarted');

  const failedSubmit = await runScenario({fetchImpl: async (path) => path === '/api/lookup'
    ? {ok: true, json: async () => ({attendee})}
    : {ok: false, json: async () => ({success: false, code: 'sheets_error',
      error: 'Google Sheets could not complete this checkpoint update.', updated_checkpoints: [],
      already_present: []})}});
  await failedSubmit.elements['#start-scan'].listeners.click();
  await failedSubmit.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  failedSubmit.checkpointOptions[1].checked = true;
  await failedSubmit.elements['#confirm-update'].listeners.click();
  assert.equal(failedSubmit.elements['#toast'].className, 'toast error');
  assert.equal(failedSubmit.elements['#toast'].textContent, 'Attendance update failed. Please retry.');
  assert.equal(failedSubmit.elements['#toast'].attributes['aria-live'], 'assertive');
  assert.equal(failedSubmit.timers.at(-1).delay, 3000, 'error toast dismisses after three seconds');
  assert.equal(failedSubmit.elements['#attendance-modal'].hidden, false, 'failed update keeps modal open');
  assert.equal(failedSubmit.checkpointOptions[1].checked, true, 'failed update keeps selection for retry');
  assert.match(failedSubmit.elements['#modal-feedback'].textContent, /could not complete/i,
    'retry detail stays visible in modal after toast timeout');
  failedSubmit.timers.at(-1).callback();
  assert.equal(failedSubmit.elements['#toast'].hidden, true);
  assert.equal(failedSubmit.elements['#modal-feedback'].hidden, false);

  const invalidFlow = await runScenario({fetchImpl: async () => ({
    ok: false, json: async () => ({error: 'not found', code: 'unknown_qr'}),
  })});
  await invalidFlow.elements['#start-scan'].listeners.click();
  await invalidFlow.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(invalidFlow.elements['#attendance-modal'].hidden, true, 'invalid QR cannot open modal');
  assert.match(invalidFlow.elements['#notice'].textContent, /QR not recognized/);
  assert.equal(invalidFlow.elements['#toast'].className, 'toast error');
  assert.equal(invalidFlow.elements['#toast'].textContent, 'QR not recognized. Please retry.');
  const toastTimerCount = invalidFlow.timers.length;
  await invalidFlow.elements['#start-scan'].listeners.click();
  await invalidFlow.instances[1].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(invalidFlow.timers.length, toastTimerCount, 'same active toast is not duplicated');
  assert.equal(invalidFlow.elements['#start-scan'].disabled, false, 'invalid QR can be retried');

  const canceled = await runScenario({fetchImpl: async () => ({ok: true, json: async () => ({attendee})})});
  await canceled.elements['#start-scan'].listeners.click();
  await canceled.instances[0].onScan('QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  canceled.checkpointOptions[2].checked = true;
  await canceled.elements['#cancel-scan'].listeners.click();
  assert.equal(canceled.elements['#attendance-modal'].hidden, true);
  assert.ok(canceled.checkpointOptions.every((option) => !option.checked), 'cancel clears modal selections');

  const unavailable = await runScenario({
    failures: [Object.assign(new Error('unavailable'), {name: 'NotFoundError'})],
  });
  await unavailable.elements['#start-scan'].listeners.click();
  assert.match(unavailable.elements['#notice'].textContent, /Rear camera unavailable/i);
  assert.equal(unavailable.elements['#switch-camera'].hidden, true, 'switch hidden after startup failure');
  assert.ok(unavailable.calls.some((entry) => entry.op === 'clear'), 'failed scanner is cleaned up');

  const laptop = await runScenario({
    failures: [Object.assign(new Error('rear missing'), {name: 'NotFoundError'})],
    cameras: [{id: 'available-camera', label: 'Integrated Camera', facingMode: 'user'}],
  });
  await laptop.elements['#start-scan'].listeners.click();
  assert.equal(startCall(laptop.calls, 1).camera, 'available-camera', 'laptop camera fallback starts');
  assert.equal(laptop.elements['#switch-camera'].hidden, false);
  console.log('scanner camera, QR, multi-checkpoint, and toast checks passed');
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
