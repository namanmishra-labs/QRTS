(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const start = document.querySelector('#start-scan');
  const stop = document.querySelector('#stop-scan');
  const switchCamera = document.querySelector('#switch-camera');
  const reader = document.querySelector('#reader');
  const mainContent = document.querySelector('main');
  const notice = document.querySelector('#notice');
  const modal = document.querySelector('#attendance-modal');
  const checkpointSection = document.querySelector('#checkpoint-section');
  const checkpointOptions = Array.from(document.querySelectorAll('input[name="checkpoint"]'));
  const presentCheckbox = document.querySelector('#mark-present');
  const modalFeedback = document.querySelector('#modal-feedback');
  const submitAttendance = document.querySelector('#confirm-update');
  let scanner = null;
  let active = false;
  let cameraBusy = false;
  let activeFacing = 'environment';
  let current = null;
  let updateInProgress = false;

  const message = (text, kind = '') => {
    notice.textContent = text;
    notice.className = `notice ${kind}`;
    notice.hidden = false;
  };
  const selectedCheckpoint = () => {
    const selected = checkpointOptions.find((option) => option.checked);
    return selected ? selected.value : '';
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
    start.disabled = cameraBusy || active || !selectedCheckpoint();
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
  function resetAttendanceForm() {
    current = null;
    modal.hidden = true;
    mainContent.inert = false;
    presentCheckbox.checked = false;
    document.querySelector('#attendee-name').textContent = '';
    document.querySelector('#attendee-id').textContent = '';
    modalFeedback.hidden = true;
    modalFeedback.textContent = '';
    submitAttendance.disabled = false;
    submitAttendance.textContent = 'Submit Attendance';
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
      presentCheckbox.checked = false;
      modalFeedback.hidden = true;
      modalFeedback.textContent = '';
      mainContent.inert = true;
      modal.hidden = false;
      submitAttendance.focus();
      message('Attendee verified. Review the details and submit attendance.', '');
    } catch (error) {
      resetAttendanceForm();
      const cameraError = error.code === 'unknown_qr' || error.code === 'invalid_qr';
      message(error.message, cameraError ? 'error' : 'warning');
    }
  }
  start.addEventListener('click', async () => {
    if (!selectedCheckpoint()) {
      message('Select one checkpoint before starting the scanner.', 'warning');
      return;
    }
    notice.hidden = true;
    start.textContent = 'Start Scanner';
    await startCamera(activeFacing);
  });
  switchCamera.addEventListener('click', async () => {
    if (!selectedCheckpoint()) {
      message('Select one checkpoint before starting the scanner.', 'warning');
      return;
    }
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
  checkpointOptions.forEach((option) => option.addEventListener('change', () => {
    updateCameraControls();
    checkpointSection.classList.toggle('checkpoint-selected', Boolean(selectedCheckpoint()));
  }));
  document.querySelector('#cancel-scan').addEventListener('click', async () => {
    if (updateInProgress) return;
    resetAttendanceForm();
    await startCamera(activeFacing);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden && !updateInProgress) {
      document.querySelector('#cancel-scan').click();
    }
  });
  submitAttendance.addEventListener('click', async () => {
    if (!current || updateInProgress || !selectedCheckpoint()) return;
    const button = submitAttendance;
    const checkpointKey = selectedCheckpoint();
    const oldStatus = current.attendee[checkpointKey];
    const newStatus = presentCheckbox.checked ? 'Present' : 'Registered';
    const selectedAttendee = current;
    updateInProgress = true;
    button.disabled = true;
    button.textContent = 'Submitting…';
    try {
      const result = await api('/api/attendance', {
        token: selectedAttendee.token, checkpoint: checkpointKey, status: newStatus, expected_status: oldStatus
      });
      resetAttendanceForm();
      start.textContent = 'Scan Next Attendee';
      message(result.duplicate ? 'This status was already recorded. Scan the next attendee when ready.' : 'Attendance updated successfully. Scan the next attendee when ready.', '');
    } catch (error) {
      modalFeedback.textContent = error.message;
      modalFeedback.className = `notice ${error.code === 'conflict' ? 'warning' : 'error'}`;
      modalFeedback.hidden = false;
      button.disabled = false;
      button.textContent = 'Submit Attendance';
    } finally { updateInProgress = false; }
  });
  document.querySelector('#logout').addEventListener('click', async () => {
    await fetch('/logout', {method: 'POST', headers: {'X-CSRF-Token': csrf}, credentials: 'same-origin'});
    window.location.assign('/');
  });
  window.addEventListener('pagehide', () => { if (scanner && active) scanner.stop().catch(() => {}); });
  checkpointSection.classList.toggle('checkpoint-selected', Boolean(selectedCheckpoint()));
  updateCameraControls();
})();
