/**
 * QRTS Sheets adapter. Deploy as a web app executing as the spreadsheet owner.
 * Every request must contain the shared server-side secret in its JSON body.
 */
const DEFAULTS = {
  HEADER_ROW: '1',
  REGISTRATION_ID_HEADER: 'Registration ID',
  NAME_HEADER: 'Name',
  EMAIL_HEADER: 'Email',
  PHONE_HEADER: 'Phone',
  QR_TOKEN_HEADER: 'QR Token',
  DAY1_MORNING_HEADER: 'Day 1 Morning',
  DAY1_EVENING_HEADER: 'Day 1 Evening',
  DAY2_MORNING_HEADER: 'Day 2 Morning',
  DAY2_EVENING_HEADER: 'Day 2 Evening'
};
const STATUSES = ['Registered', 'Present'];
const TOKEN_RE = /^QRTS1:[A-Za-z0-9-]{20,80}$/;
const EMAIL_TRACKING_HEADERS = {
  status: 'QR Email Status',
  timestamp: 'QR Email Timestamp',
  error: 'QR Email Error'
};
const EMAIL_RE = /^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/;
const CHECKPOINT_HEADERS = {
  day1_morning: 'DAY1_MORNING_HEADER',
  day1_evening: 'DAY1_EVENING_HEADER',
  day2_morning: 'DAY2_MORNING_HEADER',
  day2_evening: 'DAY2_EVENING_HEADER'
};

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const props = PropertiesService.getScriptProperties();
    const expected = props.getProperty('API_SECRET') || '';
    if (!expected || typeof body.secret !== 'string' || body.secret !== expected) {
      return json({ error: true, code: 'unauthorized' });
    }
    const action = body.action;
    if (action === 'lookup') return lookup_(body);
    if (action === 'update') return update_(body);
    if (action === 'list') return list_();
    if (action === 'qr_email_candidates') return qrEmailCandidates_();
    if (action === 'send_qr_email') return sendQrEmail_(body);
    return json({ error: true, code: 'invalid_action' });
  } catch (err) {
    // Never log request bodies, tokens, recipient addresses, or provider errors.
    console.error('QRTS request failed');
    return json({ error: true, code: 'server_error' });
  }
}

function json(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function config_() {
  const props = PropertiesService.getScriptProperties();
  const spreadsheetId = props.getProperty('SPREADSHEET_ID');
  if (!spreadsheetId) throw new Error('Missing spreadsheet configuration');
  const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  const sheetName = props.getProperty('SHEET_NAME') || '';
  const sheet = sheetName ? spreadsheet.getSheetByName(sheetName) : spreadsheet.getSheets()[0];
  if (!sheet) throw new Error('Configured sheet tab was not found');
  const names = {};
  Object.keys(DEFAULTS).forEach(key => names[key] = props.getProperty(key) || DEFAULTS[key]);
  const headerRow = Number(names.HEADER_ROW);
  if (!Number.isInteger(headerRow) || headerRow < 1) throw new Error('Invalid header row');
  const headers = sheet.getRange(headerRow, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
  const cols = {};
  Object.keys(names).filter(key => key !== 'HEADER_ROW').forEach(key => {
    const index = headers.indexOf(names[key]);
    if (index < 0) throw new Error('Required spreadsheet header missing: ' + names[key]);
    cols[key] = index + 1;
  });
  const lastRow = sheet.getLastRow();
  return { sheet, headerRow, firstDataRow: headerRow + 1, lastRow, cols };
}

function rows_(cfg) {
  if (cfg.lastRow < cfg.firstDataRow) return [];
  const values = cfg.sheet.getRange(cfg.firstDataRow, 1, cfg.lastRow - cfg.headerRow,
    cfg.sheet.getLastColumn()).getDisplayValues();
  return values.map((row, i) => ({ row, rowNumber: cfg.firstDataRow + i }));
}

function validateToken_(token) {
  return typeof token === 'string' && TOKEN_RE.test(token);
}

function findUnique_(cfg, token) {
  if (!validateToken_(token)) return { error: true, code: 'invalid_qr' };
  const matches = rows_(cfg).filter(item =>
    String(item.row[cfg.cols.REGISTRATION_ID_HEADER - 1] || '').trim() &&
    item.row[cfg.cols.QR_TOKEN_HEADER - 1] === token);
  if (!matches.length) return { error: true, code: 'not_found' };
  if (matches.length > 1) return { error: true, code: 'duplicate_token' };
  return matches[0];
}

function attendee_(cfg, found) {
  const row = found.row;
  const value = key => String(row[cfg.cols[key] - 1] || '').trim();
  return {
    registration_id: value('REGISTRATION_ID_HEADER'),
    name: value('NAME_HEADER'),
    day1_morning: value('DAY1_MORNING_HEADER') || 'Registered',
    day1_evening: value('DAY1_EVENING_HEADER') || 'Registered',
    day2_morning: value('DAY2_MORNING_HEADER') || 'Registered',
    day2_evening: value('DAY2_EVENING_HEADER') || 'Registered'
  };
}

function lookup_(body) {
  const cfg = config_();
  const found = findUnique_(cfg, body.token);
  if (found.error) return json(found);
  return json({ attendee: attendee_(cfg, found) });
}

function update_(body) {
  const checkpointKey = typeof body.checkpoint === 'string' &&
    Object.prototype.hasOwnProperty.call(CHECKPOINT_HEADERS, body.checkpoint)
    ? CHECKPOINT_HEADERS[body.checkpoint] : '';
  if (!checkpointKey || STATUSES.indexOf(body.status) < 0 || STATUSES.indexOf(body.expected_status) < 0) {
    return json({ error: true, code: 'invalid_update' });
  }
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const cfg = config_();
    const found = findUnique_(cfg, body.token);
    if (found.error) return json(found);
    const cell = cfg.sheet.getRange(found.rowNumber, cfg.cols[checkpointKey]);
    const current = String(cell.getDisplayValue() || 'Registered').trim();
    if (STATUSES.indexOf(current) < 0) return json({ error: true, code: 'invalid_current_status' });
    if (current !== body.expected_status) return json({ error: true, code: 'conflict', current_status: current });
    if (current === body.status) return json({ duplicate: true, status: current });
    cell.setValue(body.status);
    SpreadsheetApp.flush();
    return json({ updated: true, status: body.status });
  } finally {
    lock.releaseLock();
  }
}

function emailTrackingColumns_(cfg) {
  const lastCol = cfg.sheet.getLastColumn();
  const headers = cfg.sheet.getRange(cfg.headerRow, 1, 1, lastCol).getDisplayValues()[0];
  const result = {};
  Object.keys(EMAIL_TRACKING_HEADERS).forEach(key => {
    const index = headers.indexOf(EMAIL_TRACKING_HEADERS[key]);
    if (index < 0) throw new Error('Email tracking headers are not configured');
    result[key] = index + 1;
  });
  return result;
}

function eligibleEmailRows_(cfg) {
  const items = rows_(cfg);
  const seenTokens = new Set();
  const registrationCounts = {};
  items.forEach(item => {
    const id = String(item.row[cfg.cols.REGISTRATION_ID_HEADER - 1] || '').trim();
    if (id) registrationCounts[id] = (registrationCounts[id] || 0) + 1;
  });
  const eligible = [];
  items.forEach(item => {
    const row = item.row;
    const registrationId = String(row[cfg.cols.REGISTRATION_ID_HEADER - 1] || '').trim();
    const name = String(row[cfg.cols.NAME_HEADER - 1] || '').trim();
    const email = String(row[cfg.cols.EMAIL_HEADER - 1] || '').trim();
    const token = String(row[cfg.cols.QR_TOKEN_HEADER - 1] || '').trim();
    if (!registrationId || registrationCounts[registrationId] !== 1 || !name ||
        !validateToken_(token) || seenTokens.has(token)) return;
    seenTokens.add(token);
    eligible.push({ row: item.row, rowNumber: item.rowNumber, registrationId, name, email, token,
      emailValid: EMAIL_RE.test(email) });
  });
  return eligible;
}

function qrEmailCandidates_() {
  const cfg = config_();
  let tracking;
  try {
    tracking = emailTrackingColumns_(cfg);
  } catch (err) {
    return json({ error: true, code: 'email_tracking_not_configured' });
  }
  const candidates = eligibleEmailRows_(cfg).map(item => ({
    row_number: item.rowNumber,
    registration_id: item.registrationId,
    name: item.name,
    email: item.email,
    email_valid: item.emailValid,
    qr_token: item.token,
    email_status: String(cfg.sheet.getRange(item.rowNumber, tracking.status).getDisplayValue() || 'Pending').trim(),
    email_timestamp: String(cfg.sheet.getRange(item.rowNumber, tracking.timestamp).getDisplayValue() || '').trim(),
    email_error: String(cfg.sheet.getRange(item.rowNumber, tracking.error).getDisplayValue() || '').trim()
  }));
  return json({ candidates });
}

function escapeHtml_(value) {
  return String(value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function sendQrEmail_(body) {
  const registrationId = typeof body.registration_id === 'string' ? body.registration_id.trim() : '';
  const token = body.token;
  const encoded = body.qr_png_base64;
  if (!registrationId || registrationId.length > 80 || !validateToken_(token) ||
      typeof encoded !== 'string' || encoded.length > 30000 ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) {
    return json({ error: true, code: 'email_invalid_image' });
  }
  let imageBytes;
  try {
    imageBytes = Utilities.base64Decode(encoded);
    if (imageBytes.length < 100 || imageBytes.length > 20000 ||
        !encoded.startsWith('iVBORw0KGgo')) {
      return json({ error: true, code: 'email_invalid_image' });
    }
  } catch (err) {
    return json({ error: true, code: 'email_invalid_image' });
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(0)) return json({ error: true, code: 'email_in_progress' });
  try {
    const cfg = config_();
    let tracking;
    try {
      tracking = emailTrackingColumns_(cfg);
    } catch (err) {
      return json({ error: true, code: 'email_tracking_not_configured' });
    }
    const candidate = eligibleEmailRows_(cfg).find(item => item.registrationId === registrationId);
    if (!candidate || candidate.token !== token || !candidate.emailValid) {
      return json({ error: true, code: candidate ? 'email_invalid_recipient' : 'email_not_eligible' });
    }
    const rowNumber = candidate.rowNumber;
    const statusCell = cfg.sheet.getRange(rowNumber, tracking.status);
    const timestampCell = cfg.sheet.getRange(rowNumber, tracking.timestamp);
    const errorCell = cfg.sheet.getRange(rowNumber, tracking.error);
    let oldStatus = String(statusCell.getDisplayValue() || '').trim();
    if (oldStatus === 'Pending') oldStatus = '';
    const retryFailed = body.retry_failed === true;
    if (oldStatus === 'Sent') return json({ error: true, code: 'email_already_attempted', email_status: oldStatus });
    if (oldStatus === 'Sending') return json({ error: true, code: 'email_ambiguous_outcome', email_status: oldStatus });
    if (oldStatus === 'Failed' && !retryFailed) return json({ error: true, code: 'email_already_attempted', email_status: oldStatus });
    if (oldStatus && oldStatus !== 'Failed') return json({ error: true, code: 'email_already_attempted', email_status: oldStatus });
    if (retryFailed && oldStatus !== 'Failed') return json({ error: true, code: 'email_retry_not_allowed', email_status: oldStatus });

    const attemptedAt = new Date();
    statusCell.setValue('Sending');
    timestampCell.setValue(attemptedAt);
    errorCell.clearContent();
    SpreadsheetApp.flush();

    if (MailApp.getRemainingDailyQuota() < 1) {
      statusCell.setValue('Failed');
      errorCell.setValue('quota_exhausted');
      SpreadsheetApp.flush();
      return json({ error: true, code: 'email_quota_exhausted', email_status: 'Failed' });
    }
    let mailAccepted = false;
    try {
      const blob = Utilities.newBlob(imageBytes, 'image/png', 'attendee-qr.png');
      const safeName = escapeHtml_(candidate.name);
      MailApp.sendEmail({
        to: candidate.email,
        subject: 'Your QR code for Startup Conclave 2026',
        body: 'Hello ' + candidate.name + ', Registration ID: ' + candidate.registrationId +
          '. Your individual QR code is attached. Please keep it available for event check-in.',
        htmlBody: '<p>Hello ' + safeName + ',</p><p>Registration ID: <strong>' + escapeHtml_(candidate.registrationId) +
          '</strong></p><p>Your individual attendee QR code is below. Please keep it available for event check-in.</p><p><img src="cid:attendeeQr" alt="Your attendee QR code"></p>',
        inlineImages: { attendeeQr: blob },
        attachments: [blob]
      });
      mailAccepted = true;
    } catch (err) {
      statusCell.setValue('Failed');
      errorCell.setValue('delivery_failed');
      SpreadsheetApp.flush();
      return json({ error: true, code: 'email_delivery_failed', email_status: 'Failed' });
    }
    if (mailAccepted) {
      try {
        statusCell.setValue('Sent');
        errorCell.clearContent();
        SpreadsheetApp.flush();
        return json({ sent: true, email_status: 'Sent', email_timestamp: attemptedAt.toISOString() });
      } catch (err) {
        // MailApp accepted the message, but status persistence failed. Leave Sending
        // as a review lock so a retry cannot create an accidental duplicate.
        try { errorCell.setValue('outcome_unknown'); SpreadsheetApp.flush(); } catch (_) {}
        return json({ error: true, code: 'email_ambiguous_outcome', email_status: 'Sending' });
      }
    }
  } finally {
    lock.releaseLock();
  }
}

function list_() {
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const cfg = config_();
    const items = rows_(cfg);
    const tokenCol = cfg.cols.QR_TOKEN_HEADER;
    const used = new Set();
    items.forEach(item => {
      const existing = String(item.row[tokenCol - 1] || '').trim();
      if (existing && (!validateToken_(existing) || used.has(existing))) {
        throw new Error('Existing QR token is invalid or duplicated');
      }
      if (existing) used.add(existing);
    });
    items.forEach(item => {
      const existing = String(item.row[tokenCol - 1] || '').trim();
      const registrationId = String(item.row[cfg.cols.REGISTRATION_ID_HEADER - 1] || '').trim();
      if (registrationId) {
        ['DAY1_MORNING_HEADER', 'DAY1_EVENING_HEADER',
          'DAY2_MORNING_HEADER', 'DAY2_EVENING_HEADER'].forEach(key => {
          const col = cfg.cols[key];
          const status = String(item.row[col - 1] || '').trim();
          if (!status) {
            cfg.sheet.getRange(item.rowNumber, col).setValue('Registered');
            item.row[col - 1] = 'Registered';
          } else if (STATUSES.indexOf(status) < 0) {
            throw new Error('Attendance column contains an unsupported status');
          }
        });
      }
      if (registrationId && !existing) {
        let token;
        do { token = 'QRTS1:' + Utilities.getUuid(); } while (used.has(token));
        cfg.sheet.getRange(item.rowNumber, tokenCol).setValue(token);
        item.row[tokenCol - 1] = token;
        used.add(token);
      }
    });
    SpreadsheetApp.flush();
    return json({ attendees: items.filter(item =>
      String(item.row[cfg.cols.REGISTRATION_ID_HEADER - 1] || '').trim() && item.row[tokenCol - 1]
    ).map(item => ({
      registration_id: String(item.row[cfg.cols.REGISTRATION_ID_HEADER - 1] || '').trim(),
      name: String(item.row[cfg.cols.NAME_HEADER - 1] || '').trim(),
      qr_token: item.row[tokenCol - 1]
    })) });
  } finally {
    lock.releaseLock();
  }
}
