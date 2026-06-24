/**
 * ONE-TIME: paste this into your Google Sheet
 * Extensions → Apps Script → paste → Save
 *
 * Deploy → New deployment → Web app
 *   Execute as: Me
 *   Who has access: Anyone
 * Copy the Web App URL into google_sheets_webapp.json (see 3_sync_to_sheet.py)
 */
const SPREADSHEET_ID = "1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE";
const SECRET = "pinterest-scan-2026"; // must match google_sheets_webapp.json

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    if (data.secret !== SECRET) {
      return jsonOut({ ok: false, error: "unauthorized" });
    }

    const statuses = data.statuses || [];
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheets()[0];

    sheet.getRange(1, 4).setValue("Status");
    if (statuses.length > 0) {
      const values = statuses.map(function (s) { return [s]; });
      sheet.getRange(2, 4, statuses.length, 1).setValues(values);
    }

    return jsonOut({ ok: true, count: statuses.length });
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}

function jsonOut(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}