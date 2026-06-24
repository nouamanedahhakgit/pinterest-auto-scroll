/**
 * HOW TO DEPLOY (follow exactly):
 * 1. Google Sheet → Extensions → Apps Script
 * 2. Delete ALL files/code in the editor
 * 3. Paste THIS ENTIRE FILE (must include function doPost at the top)
 * 4. Save
 * 5. Deploy → New deployment → Type: Web app
 *      Execute as: Me | Who has access: Anyone
 * 6. Copy the /exec URL into google_sheets_webapp.json
 *
 * Test in browser: open the /exec URL → should show {"ok":true,"version":2,...}
 */
const SPREADSHEET_ID = "1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE";
const SECRET = "pinterest-scan-2026";

function doGet() {
  return jsonOut({ ok: true, version: 2, message: "Web app ready — doPost exists" });
}

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    if (data.secret !== SECRET) {
      return jsonOut({ ok: false, error: "unauthorized" });
    }

    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheets()[0];

    if (data.action === "setup" && data.rows && data.rows.length > 0) {
      return setupKeywords(sheet, data.rows);
    }

    return writeColumn(sheet, data);
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}

function setupKeywords(sheet, rows) {
  const header = [["Keyword", "Pinterest Search URL", "Pinterest Trends URL", "Status"]];
  const all = header.concat(rows);
  sheet.clearContents();
  if (all.length > 0) {
    sheet.getRange("A1:D" + all.length).setValues(all);
  }
  return jsonOut({ ok: true, action: "setup", count: rows.length });
}

function writeColumn(sheet, data) {
  const col = data.column || 4;
  const values = (data.statuses || []).map(function (s) { return [s]; });
  const colLetter = ["", "A", "B", "C", "D"][col];

  if (data.set_header) {
    sheet.getRange("A1:D1").setValues([
      ["Keyword", "Pinterest Search URL", "Pinterest Trends URL", "Status"]
    ]);
  } else if (col === 4) {
    sheet.getRange("D1").setValue("Status");
  }

  if (values.length > 0) {
    sheet.getRange(colLetter + "2:" + colLetter + (values.length + 1)).setValues(values);
  }

  return jsonOut({
    ok: true,
    action: col === 4 && !data.set_header ? "sync" : "column",
    column: col,
    count: values.length
  });
}

function jsonOut(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}