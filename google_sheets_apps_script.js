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
  return jsonOut({ ok: true, version: 3, message: "Web app ready — claim/mark supported" });
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
    if (data.action === "claim") {
      return claimKeywords(sheet, data.count || 5);
    }
    if (data.action === "mark") {
      return markKeywords(sheet, data.keywords || [], data.status || "Done");
    }
    if (data.action === "sync_websites") {
      return syncWebsites(ss, data.rows || []);
    }

    return writeColumn(sheet, data);
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}

// Atomically claim up to n keywords whose Status is empty/"Not Yet": set them to
// "pending" and return them. LockService serialises concurrent computers so the
// same keyword is never handed to two machines.
function claimKeywords(sheet, n) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const last = sheet.getLastRow();
    if (last < 2) return jsonOut({ ok: true, action: "claim", claimed: [] });
    const vals = sheet.getRange(2, 1, last - 1, 4).getValues();   // A2:D(last)
    const claimed = [];
    for (let i = 0; i < vals.length && claimed.length < n; i++) {
      const kw = (vals[i][0] || "").toString().trim();
      const st = (vals[i][3] || "").toString().trim().toLowerCase();
      if (kw && (st === "" || st === "not yet")) {
        vals[i][3] = "pending";
        claimed.push({ row: i + 2, keyword: kw });
      }
    }
    if (claimed.length) {
      sheet.getRange(2, 4, vals.length, 1).setValues(vals.map(function (r) { return [r[3]]; }));
      SpreadsheetApp.flush();
    }
    return jsonOut({ ok: true, action: "claim", claimed: claimed });
  } finally {
    lock.releaseLock();
  }
}

// Set Status for the given keywords (column A match) to `status` (e.g. "Done").
function markKeywords(sheet, keywords, status) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const last = sheet.getLastRow();
    if (last < 2) return jsonOut({ ok: true, action: "mark", count: 0 });
    const vals = sheet.getRange(2, 1, last - 1, 4).getValues();
    const want = {};
    keywords.forEach(function (k) { want[(k || "").toString().trim().toLowerCase()] = 1; });
    let count = 0;
    for (let i = 0; i < vals.length; i++) {
      const kw = (vals[i][0] || "").toString().trim().toLowerCase();
      if (want[kw]) { vals[i][3] = status; count++; }
    }
    sheet.getRange(2, 4, vals.length, 1).setValues(vals.map(function (r) { return [r[3]]; }));
    SpreadsheetApp.flush();
    return jsonOut({ ok: true, action: "mark", count: count });
  } finally {
    lock.releaseLock();
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

function syncWebsites(ss, rows) {
  let wsSheet = ss.getSheetByName("websites");
  if (!wsSheet) {
    wsSheet = ss.insertSheet("websites");
    wsSheet.appendRow(["id", "website", "scrapped"]);
  }
  
  const last = wsSheet.getLastRow();
  let existingIds = {};
  if (last > 1) {
    const ids = wsSheet.getRange(2, 1, last - 1, 1).getValues();
    ids.forEach(function(r) {
      const idVal = String(r[0]).trim();
      if (idVal) {
        existingIds[idVal.toLowerCase()] = true;
      }
    });
  }
  
  const newRows = [];
  rows.forEach(function(row) {
    const pinnerId = String(row[0]).trim();
    if (pinnerId && !existingIds[pinnerId.toLowerCase()]) {
      newRows.push([pinnerId, row[1] || "", row[2] || "not yet"]);
    }
  });
  
  if (newRows.length > 0) {
    wsSheet.getRange(last + 1, 1, newRows.length, 3).setValues(newRows);
    SpreadsheetApp.flush();
  }
  return jsonOut({ ok: true, action: "sync_websites", count: newRows.length });
}

function jsonOut(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}