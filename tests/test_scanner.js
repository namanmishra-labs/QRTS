const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class FakeElement {
  constructor() {
    this.listeners = {};
    this.classNames = new Set();
    this.disabled = false;
    this.hidden = false;
    this.video = null;
    this.textContent = '';
    this.className = '';
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) { return selector === 'video' ? this.video : null; }
  scrollIntoView() {}
  get classList() {
    return {
      add: (name) => this.classNames.add(name),
      remove: (name) => this.classNames.delete(name),
    };
  }
}

async function runScenario({failures = [], cameras = [], makeVideo = true} = {}) {
  const ids = [
    'start-scan', 'stop-scan', 'switch-camera', 'notice', 'attendee-card', 'checkpoint',
    'reader', 'camera-help', 'current-status', 'confirm-update', 'logout',
    'new-status', 'attendee-name', 'attendee-id',
  ];
  const elements = Object.fromEntries(ids.map((id) => [`#${id}`, new FakeElement()]));
  elements['#checkpoint'].value = 'day1_morning';
  const reader = elements['#reader'];
  const calls = [];
  const instances = [];
  class MockScanner {
    constructor() { this.id = instances.length + 1; instances.push(this); }
    async start(camera, _config, _success, _error) {
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
        ? {content: 'test-csrf'} : elements[selector],
      querySelectorAll: () => [],
    },
    window: {
      Html5Qrcode: MockScanner,
      setTimeout,
      addEventListener() {},
      confirm: () => true,
      location: {assign() {}},
    },
    navigator: {mediaDevices: {getUserMedia: async () => { throw new Error('library handles camera access'); }}},
    Html5Qrcode: MockScanner,
    fetch: async () => ({ok: true, json: async () => ({})}),
    setTimeout,
    clearTimeout,
    Date,
    Promise,
    Error,
    JSON,
  };
  vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
  return {elements, calls, instances};
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
  console.log('scanner camera switching checks passed');
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
