// Morning Log inbox: a Google Sheet that receives daily feeling scores from
// the Android app (doPost) and hands them to `wearable log-sync` (doGet).
//
// Don't paste this template directly. Run `uv run wearable log-inbox` — it
// writes data/self_report_inbox.gs with your token filled in and prints the
// deploy steps. The token is the only thing keeping strangers out of the
// web app, so never commit the filled-in copy.

const TOKEN = '__SELF_REPORT_TOKEN__';
const SHEET_NAME = 'self_report';
const HEADER = ['date', 'readiness', 'logged_at', 'received_at'];

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, error: 'body is not JSON' });
  }
  if (body.token !== TOKEN) return json_({ ok: false, error: 'unauthorized' });

  const date = String(body.date || '');
  const readiness = Number(body.readiness);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return json_({ ok: false, error: 'date must be YYYY-MM-DD' });
  }
  if (!Number.isInteger(readiness) || readiness < 1 || readiness > 100) {
    return json_({ ok: false, error: 'readiness must be an integer 1-100' });
  }
  const row = [date, readiness, String(body.logged_at || ''), new Date().toISOString()];

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sheet = sheet_();
    const values = sheet.getDataRange().getValues();
    // One row per date: re-logging a day overwrites it, like `wearable log`.
    for (let i = 1; i < values.length; i++) {
      if (text_(values[i][0]) === date) {
        sheet.getRange(i + 1, 1, 1, HEADER.length).setValues([row]);
        return json_({ ok: true, updated: true });
      }
    }
    sheet.appendRow(row);
    return json_({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  const params = e.parameter || {};
  if (params.token !== TOKEN) return json_({ ok: false, error: 'unauthorized' });
  const since = params.since || '';
  const rows = sheet_()
    .getDataRange()
    .getValues()
    .slice(1)
    .map((r) => ({ date: text_(r[0]), readiness: Number(r[1]), logged_at: text_(r[2]) }))
    .filter((r) => r.date && r.date >= since);
  return json_({ ok: true, rows: rows });
}

function sheet_() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = book.insertSheet(SHEET_NAME);
    // Plain-text columns, or Sheets silently turns "2026-11-08" into a Date.
    sheet.getRange('A:A').setNumberFormat('@');
    sheet.getRange('C:D').setNumberFormat('@');
    sheet.appendRow(HEADER);
  }
  return sheet;
}

// Cells typed by hand can still become Dates; normalize back to text.
function text_(value) {
  if (value instanceof Date) {
    return Utilities.formatDate(value, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }
  return String(value);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}
