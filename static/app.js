(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const start = document.querySelector('#start-scan');
  const stop = document.querySelector('#stop-scan');
  const switchCamera = document.querySelector('#switch-camera');
  const reader = document.querySelector('#reader');
  const mainContent = document.querySelector('main');
  const notice = document.querySelector('#notice');
  const modal = document.querySelector('#attendance-modal');
  const checkpointOptions = Array.from(document.querySelectorAll('input[name="attendance-checkpoint"]'));
  const modalFeedback = document.querySelector('#modal-feedback');
  const submitAttendance = document.querySelector('#confirm-update');
  const toast = document.querySelector('#toast');
  const checkpointLabels = {
    day1_morning: 'Day 1 — Morning', day1_evening: 'Day 1 — Evening',
    day2_morning: 'Day 2 — Morning', day2_evening: 'Day 2 — Evening',
  };
  let scanner = null;
  let active = false;
  let cameraBusy = false;
  let activeFacing = 'environment';
  let previewReady = false;
  let current = null;
  let updateInProgress = false;
  let toastTimer = null;
  let activeToastKey = '';

  const message = (text, kind = '') => {
    notice.textContent = text;
    notice.className = `notice ${kind}`;
    notice.hidden = false;
  };
  const showToast = (text, kind) => {
    const key = `${kind}:${text}`;
    if (!toast.hidden && key === activeToastKey) return;
    if (toastTimer !== null) window.clearTimeout(toastTimer);
    activeToastKey = key;
    toast.textContent = text;
    toast.className = `toast ${kind}`;
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    toast.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
    toast.hidden = false;
    toastTimer = window.setTimeout(() => {
      toast.hidden = true;
      activeToastKey = '';
      toastTimer = null;
    }, 3000);
  };
  const selectedCheckpointKeys = () => checkpointOptions
    .filter((option) => option.checked).map((option) => option.value);

  async function api(path, data) {
    const response = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify(data), credentials: 'same-origin'
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(result.error || 'Request failed. Try again.'),
      {code: result.code, result});
    return result;
  }

  function updateCameraControls() {
    start.disabled = cameraBusy || active;
    stop.disabled = cameraBusy || !active;
    switchCamera.hidden = cameraBusy || !active || !previewReady;
    switchCamera.disabled = cameraBusy || !previewReady;
    switchCamera.textContent = 'Change Camera';
  }

  async function stopScanner() {
    const oldScanner = scanner;
    const wasActive = active;
    scanner = null;
    active = false;
    previewReady = false;
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
      message('The QR scanner could not load. Check your internet connection and reload.', 'error');
      return;
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      message('This browser does not provide camera access. Open the site in a supported browser over HTTPS.', 'error');
      return;
    }
    cameraBusy = true;
    previewReady = false;
    updateCameraControls();
    reader.classList.add('scanner-active');
    message(active ? 'Switching camera…' : 'Starting camera…');
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
        try { cameras = await Html5Qrcode.getCameras(); } catch (_) { /* report original error */ }
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
      previewReady = true;
      updateCameraControls();
      message(usedFallback
        ? 'Rear camera unavailable. An available camera is running.'
        : `${activeFacing === 'user' ? 'Front' : 'Back'} camera ready.`, '');
    } catch (error) {
      await stopScanner();
      updateCameraControls();
      let explanation = 'Could not start the camera. Check browser camera permission and try again.';
      if (['preview_missing', 'stream_not_live', 'preview_not_ready'].includes(error.message)) {
        explanation = 'The camera did not provide a live preview. Check camera availability and try again.';
      } else if (error.name === 'NotAllowedError' || /permission|denied/i.test(String(error))) {
        explanation = 'Camera permission was denied. Allow camera access in the browser settings, then try again.';
      } else if (error.name === 'NotFoundError' || /camera.*not found/i.test(String(error))) {
        explanation = facing === 'environment'
          ? 'Rear camera unavailable. Try Start Scanner again or switch to an available camera.'
          : 'Front camera unavailable. Try Start Scanner again.';
      } else if (error.name === 'NotReadableError' || /in use|not readable/i.test(String(error))) {
        explanation = 'The camera may be in use by another app. Close it and try again.';
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
    checkpointOptions.forEach((option) => { option.checked = false; });
    document.querySelector('#attendee-name').textContent = '';
    document.querySelector('#attendee-id').textContent = '';
    modalFeedback.hidden = true;
    modalFeedback.textContent = '';
    submitAttendance.disabled = false;
    submitAttendance.textContent = 'Submit Attendance';
  }

  async function handleScan(decoded) {
    if (!active || cameraBusy || updateInProgress) return;
    cameraBusy = true;
    updateCameraControls();
    await stopScanner();
    message('Verifying QR code…');
    try {
      const result = await api('/api/lookup', {token: decoded});
      current = {token: decoded, attendee: result.attendee};
      document.querySelector('#attendee-name').textContent = result.attendee.name;
      document.querySelector('#attendee-id').textContent = result.attendee.registration_id;
      checkpointOptions.forEach((option) => { option.checked = false; });
      modalFeedback.hidden = true;
      modalFeedback.textContent = '';
      mainContent.inert = true;
      modal.hidden = false;
      document.querySelector('#cancel-scan').focus();
    } catch (error) {
      resetAttendanceForm();
      const cameraError = error.code === 'unknown_qr' || error.code === 'invalid_qr';
      const errorText = cameraError ? 'QR not recognized. Start Scanner to try again.'
        : 'QR verification failed. Please retry.';
      message(errorText, cameraError ? 'error' : 'warning');
      showToast(cameraError ? 'QR not recognized. Please retry.' : errorText, 'error');
    } finally {
      cameraBusy = false;
      updateCameraControls();
    }
  }

  start.addEventListener('click', async () => {
    notice.hidden = true;
    await startCamera(activeFacing);
  });
  switchCamera.addEventListener('click', async () => {
    if (cameraBusy || !previewReady || !active) return;
    await startCamera(activeFacing === 'user' ? 'environment' : 'user');
  });
  stop.addEventListener('click', async () => {
    if (cameraBusy) return;
    cameraBusy = true;
    updateCameraControls();
    await stopScanner();
    cameraBusy = false;
    updateCameraControls();
    message('Scanner stopped. Start Scanner when ready.');
  });
  document.querySelector('#cancel-scan').addEventListener('click', () => {
    if (updateInProgress) return;
    resetAttendanceForm();
    message('Attendance cancelled. Start Scanner when ready.');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden && !updateInProgress) {
      document.querySelector('#cancel-scan').click();
    }
  });
  submitAttendance.addEventListener('click', async () => {
    if (!current || updateInProgress) return;
    const selected = selectedCheckpointKeys();
    if (!selected.length) {
      modalFeedback.textContent = 'Select at least one checkpoint.';
      modalFeedback.className = 'notice warning';
      modalFeedback.hidden = false;
      showToast('Select at least one checkpoint.', 'error');
      return;
    }
    const attendee = current;
    const checkpoints = selected.map((checkpoint) => ({
      checkpoint, expected_status: attendee.attendee[checkpoint],
    }));
    updateInProgress = true;
    submitAttendance.disabled = true;
    submitAttendance.textContent = 'Submitting…';
    modalFeedback.hidden = true;
    try {
      const result = await api('/api/attendance', {token: attendee.token, checkpoints});
      const completed = [...(result.updated_checkpoints || []), ...(result.already_present || [])];
      if (result.success !== true || completed.length !== selected.length) {
        throw Object.assign(new Error('Attendance update was not confirmed. Please retry.'), {
          result: {updated_checkpoints: result.updated_checkpoints || [],
            already_present: result.already_present || [], code: 'incomplete_result'},
        });
      }
      const count = completed.length;
      resetAttendanceForm();
      const successText = count === 1 ? 'Marked Present Successfully'
        : `Attendance marked Present for ${count} checkpoints`;
      message(successText, '');
      showToast(successText, 'success');
    } catch (error) {
      const result = error.result || {};
      if (result.code === 'conflict' && result.failed_checkpoint &&
          ['Registered', 'Present'].includes(result.current_status)) {
        attendee.attendee[result.failed_checkpoint] = result.current_status;
      }
      const completed = [...(result.updated_checkpoints || []), ...(result.already_present || [])];
      const completedText = completed.length
        ? `${completed.length} checkpoint(s) were already updated: ${completed.map((key) => checkpointLabels[key] || 'selected checkpoint').join(', ')}. `
        : '';
      const failedLabel = checkpointLabels[result.failed_checkpoint] || 'A selected checkpoint';
      modalFeedback.textContent = result.partial
        ? `Partial update. ${completedText}${failedLabel} could not be updated. Retry is safe; completed checkpoints will not be changed again.`
        : `${error.message} ${completedText}${result.code === 'conflict' ? 'Review the attendance and retry.' : ''}`.trim();
      modalFeedback.className = `notice ${result.code === 'conflict' ? 'warning' : 'error'}`;
      modalFeedback.hidden = false;
      const completedCount = completed.length;
      const toastText = result.partial && completedCount
        ? `Partial update: ${completedCount} checkpoint(s) saved. Please retry.`
        : result.code === 'conflict' ? 'Attendance changed. Please review and retry.'
          : 'Attendance update failed. Please retry.';
      showToast(toastText, 'error');
      submitAttendance.disabled = false;
      submitAttendance.textContent = 'Submit Attendance';
    } finally {
      updateInProgress = false;
    }
  });

  window.addEventListener('pagehide', () => {
    if (scanner && active) scanner.stop().catch(() => {});
  });
  updateCameraControls();
})();
