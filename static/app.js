(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const start = document.querySelector('#start-scan');
  const stop = document.querySelector('#stop-scan');
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
  function stopScanner() {
    if (!scanner || !active) return Promise.resolve();
    active = false;
    stop.disabled = true;
    start.disabled = false;
    return scanner.stop().catch(() => {}).then(() => scanner.clear()).catch(() => {});
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
    if (!active) return;
    await stopScanner();
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
    card.hidden = true;
    if (!window.Html5Qrcode) {
      message('The QR scanner could not load. Check your internet connection and reload.', 'error'); return;
    }
    scanner = new Html5Qrcode('reader', {verbose: false});
    start.disabled = true;
    try {
      await scanner.start({facingMode: 'environment'}, {fps: 10, qrbox: {width: 250, height: 250}}, handleScan, () => {});
      active = true;
      stop.disabled = false;
      message('Scanner ready. Hold the attendee QR steady inside the frame.');
    } catch (error) {
      active = false;
      start.disabled = false;
      stop.disabled = true;
      let explanation = 'Could not start the camera. Check browser camera permission and try again.';
      if (error.name === 'NotAllowedError' || /permission|denied/i.test(String(error))) {
        explanation = 'Camera permission was denied. In Android Chrome, open the site settings from the lock/tune icon, allow Camera, then reload this page.';
      } else if (error.name === 'NotFoundError' || /camera.*not found/i.test(String(error))) {
        explanation = 'No camera was found. Connect or enable a camera, then try again.';
      } else if (error.name === 'NotReadableError' || /in use|not readable/i.test(String(error))) {
        explanation = 'The camera may be in use by another app. Close other camera apps and try again.';
      }
      message(explanation, 'error');
    }
  });
  stop.addEventListener('click', async () => { await stopScanner(); message('Scanner stopped. Start it again when ready.'); });
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
})();
