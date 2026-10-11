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
    return json({ error: true, code: 'invalid_action' });
  } catch (err) {
    console.error('QRTS request failed: ' + String(err));
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
