(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const start = document.querySelector('#start-scan');
  const stop = document.querySelector('#stop-scan');
  const switchCamera = document.querySelector('#switch-camera');
  const reader = document.querySelector('#reader');
  const notice = document.querySelector('#notice');
  const card = document.querySelector('#attendee-card');
  const checkpoint = document.querySelector('#checkpoint');
  const attendanceWarning = document.querySelector('#existing-attendance-warning');
  const checkpointLabels = {
    day1_morning: 'Day 1 – Morning',
    day1_evening: 'Day 1 – Evening',
    day2_morning: 'Day 2 – Morning',
    day2_evening: 'Day 2 – Evening'
  };
  let scanner = null;
  let active = false;
  let cameraBusy = false;
  let activeFacing = 'environment';
  let current = null;

  const message = (text, kind = '') => {
    notice.textContent = text;
    notice.className = `notice ${kind}`;
    notice.hidden = false;
  };
  async function api(path, data) {
    const response = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify(data), credentials: 'same-origin'
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(result.error || 'Request failed. Try again.'), {code: result.code, result});
    return result;
  }
  function updateCameraControls() {
    start.disabled = cameraBusy || active;
    stop.disabled = cameraBusy || !active;
    switchCamera.disabled = cameraBusy;
    switchCamera.textContent = activeFacing === 'user'
      ? 'Switch to Back Camera' : 'Switch to Front Camera';
  }
  async function stopScanner() {
    const oldScanner = scanner;
    const wasActive = active;
    scanner = null;
    active = false;
    if (oldScanner) {
      if (wasActive) {
        try { await oldScanner.stop(); } catch (_) { /* continue cleanup */ }
      }
      try { await oldScanner.clear(); } catch (_) { /* scanner may not have started */ }
    }
    reader.classList.remove('scanner-active');
  }
  async function verifyCameraPreview() {
    const video = reader.querySelector('video');
    if (!video) throw new Error('preview_missing');
    if (!video.srcObject || typeof video.srcObject.getVideoTracks !== 'function' ||
        !video.srcObject.getVideoTracks().some((track) => track.readyState === 'live')) {
      throw new Error('stream_not_live');
    }
    await video.play();
    const deadline = Date.now() + 5000;
    while (video.readyState < 2 || !video.videoWidth || !video.videoHeight || video.paused) {
      if (Date.now() >= deadline) throw new Error('preview_not_ready');
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    return video;
  }
  function facingFromPreview(video, fallbackCamera, requestedFacing) {
    const track = video.srcObject.getVideoTracks()[0];
    const reported = track && track.getSettings && track.getSettings().facingMode;
    if (reported === 'user' || reported === 'environment') return reported;
    const label = fallbackCamera && fallbackCamera.label || '';
    if (/front|user|integrated|built[- ]?in|facetime/i.test(label)) return 'user';
    if (/rear|back|environment/i.test(label)) return 'environment';
    return requestedFacing;
  }
  async function startCamera(facing) {
    if (cameraBusy) return;
    if (!window.Html5Qrcode) {
      message('The QR scanner could not load. Check your internet connection and reload.', 'error'); return;
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      message('This browser does not provide camera access. Open the site in a supported browser over HTTPS.', 'error'); return;
    }
    cameraBusy = true;
    updateCameraControls();
    card.hidden = true;
    reader.classList.add('scanner-active');
    message(facing === activeFacing && active ? 'Restarting camera…' : 'Starting camera…', '');
    let fallbackCamera = null;
    let usedFallback = false;
    try {
      await stopScanner();
      reader.classList.add('scanner-active');
      try {
        await launchScanner({facingMode: facing});
      } catch (preferredError) {
        if (facing !== 'environment' || preferredError.name === 'NotAllowedError' ||
            preferredError.name === 'SecurityError') throw preferredError;
        let cameras = [];
        try { cameras = await Html5Qrcode.getCameras(); } catch (_) { /* show original error below */ }
        fallbackCamera = cameras.find((camera) => camera && camera.id &&
          /front|user|integrated|built[- ]?in|facetime/i.test(camera.label || '')) ||
          cameras.find((camera) => camera && camera.id);
        if (!fallbackCamera) throw preferredError;
        await stopScanner();
        reader.classList.add('scanner-active');
        await launchScanner(fallbackCamera.id);
        usedFallback = true;
      }
      const video = await verifyCameraPreview();
      activeFacing = facingFromPreview(video, fallbackCamera, facing);
      updateCameraControls();
      message(usedFallback
        ? 'The rear camera was unavailable. The available camera is running; use the switch button to try the front camera.'
        : `${activeFacing === 'user' ? 'Front' : 'Back'} camera ready. Hold the attendee QR steady inside the frame.`, '');
    } catch (error) {
      await stopScanner();
      updateCameraControls();
      let explanation = 'Could not start the camera. Check browser camera permission and try again.';
      if (error.message === 'preview_missing' || error.message === 'stream_not_live' ||
          error.message === 'preview_not_ready') {
        explanation = 'The camera did not provide a live video preview. Check that the camera is available, close other camera apps, then try again.';
      } else if (error.name === 'NotAllowedError' || /permission|denied/i.test(String(error))) {
        explanation = 'Camera permission was denied. In Android Chrome, open the site settings from the lock/tune icon, allow Camera, then reload this page.';
      } else if (error.name === 'NotFoundError' || /camera.*not found/i.test(String(error))) {
        explanation = facing === 'environment'
          ? 'The rear camera could not start. Try Switch to Front Camera or connect/enable a camera.'
          : 'No front camera was found. Switch back or connect/enable a camera.';
      } else if (error.name === 'NotReadableError' || /in use|not readable/i.test(String(error))) {
        explanation = 'The camera may be in use by another app. Close other camera apps and try again.';
      } else if (facing === 'environment') {
        explanation = 'The rear camera could not start. Try Switch to Front Camera or use the available-camera fallback.';
      }
      message(explanation, 'error');
    } finally {
      cameraBusy = false;
      updateCameraControls();
    }
  }
  async function launchScanner(cameraConfig) {
    scanner = new Html5Qrcode('reader', {verbose: false});
    await scanner.start(cameraConfig, {fps: 10, qrbox: {width: 250, height: 250}}, handleScan, () => {});
    active = true;
    updateCameraControls();
  }
  function renderAttendanceStatus() {
    if (!current) return;
    const status = current.attendee[checkpoint.value];
    document.querySelector('#current-status').textContent = status;
    document.querySelectorAll('[data-attendance-status]').forEach((element) => {
      element.textContent = current.attendee[element.dataset.attendanceStatus];
    });
    const alreadyRecorded = status !== 'Registered';
    attendanceWarning.hidden = !alreadyRecorded;
    attendanceWarning.textContent = alreadyRecorded
      ? `${checkpointLabels[checkpoint.value]} की attendance पहले से दर्ज है। वर्तमान status: ${status}.`
      : `${checkpointLabels[checkpoint.value]} के लिए अभी attendance दर्ज नहीं है।`;
  }
  async function handleScan(decoded) {
    if (!active || cameraBusy) return;
    cameraBusy = true;
    updateCameraControls();
    await stopScanner();
    cameraBusy = false;
    updateCameraControls();
    try {
      const result = await api('/api/lookup', {token: decoded});
      current = {token: decoded, attendee: result.attendee};
      document.querySelector('#attendee-name').textContent = result.attendee.name;
      document.querySelector('#attendee-id').textContent = result.attendee.registration_id;
      document.querySelector('#new-status').value = result.attendee[checkpoint.value];
      renderAttendanceStatus();
      card.hidden = false;
      card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      message('Attendee verified. Review the selected checkpoint and status before confirming.', '');
    } catch (error) {
      current = null;
      card.hidden = true;
      const cameraError = error.code === 'unknown_qr' || error.code === 'invalid_qr';
      message(error.message, cameraError ? 'error' : 'warning');
    }
  }
  start.addEventListener('click', async () => {
    notice.hidden = true;
    await startCamera(activeFacing);
  });
  switchCamera.addEventListener('click', async () => {
    const requestedFacing = activeFacing === 'user' ? 'environment' : 'user';
    await startCamera(requestedFacing);
  });
  stop.addEventListener('click', async () => {
    if (cameraBusy) return;
    cameraBusy = true;
    updateCameraControls();
    await stopScanner();
    cameraBusy = false;
    updateCameraControls();
    message('Scanner stopped. Start it again when ready.');
  });
  checkpoint.addEventListener('change', () => {
    if (current) {
      renderAttendanceStatus();
      document.querySelector('#new-status').value = current.attendee[checkpoint.value];
    }
  });
  document.querySelector('#confirm-update').addEventListener('click', async (event) => {
    if (!current) return;
    const button = event.currentTarget;
    const oldStatus = current.attendee[checkpoint.value];
    const newStatus = document.querySelector('#new-status').value;
    if (oldStatus === newStatus) { message('No change: this attendee already has that status.', 'warning'); return; }
    const changingExistingStatus = oldStatus !== 'Registered';
    if (changingExistingStatus && !window.confirm(
      `${checkpointLabels[checkpoint.value]} attendance is already ${oldStatus} for ${current.attendee.name}. Change it to ${newStatus}?`
    )) return;
    button.disabled = true;
    try {
      const result = await api('/api/attendance', {
        token: current.token, checkpoint: checkpoint.value, status: newStatus, expected_status: oldStatus,
        confirm_status_change: changingExistingStatus
      });
      current.attendee[checkpoint.value] = result.status;
      renderAttendanceStatus();
      message(result.duplicate ? 'This attendance was already recorded.' : 'Attendance updated successfully.', '');
    } catch (error) {
      message(error.message, error.code === 'conflict' ? 'warning' : 'error');
      if (error.result && error.result.current_status) {
        current.attendee[checkpoint.value] = error.result.current_status;
        renderAttendanceStatus();
      }
    } finally { button.disabled = false; }
  });
  document.querySelector('#logout').addEventListener('click', async () => {
    await fetch('/logout', {method: 'POST', headers: {'X-CSRF-Token': csrf}, credentials: 'same-origin'});
    window.location.assign('/');
  });
  window.addEventListener('pagehide', () => { if (scanner && active) scanner.stop().catch(() => {}); });
  updateCameraControls();
})();
