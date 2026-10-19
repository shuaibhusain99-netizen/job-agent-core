/**
 * Google Sheet tracker endpoint for the job agent.
 *
 * Setup (once, ~3 minutes):
 *  1. Open your tracker Google Sheet → Extensions → Apps Script.
 *  2. Delete the sample code, paste this file, set TOKEN to a long random string.
 *  3. Deploy → New deployment → Type: Web app → Execute as: Me → Who has access: Anyone → Deploy.
 *     Authorise when asked. Copy the Web app URL.
 *  4. In GitHub → Settings → Secrets and variables → Actions add:
 *        SHEET_WEBHOOK_URL   = the Web app URL
 *        SHEET_WEBHOOK_TOKEN = the same TOKEN string
 *
 * The agent POSTs {token, rows:[[...]]}; rows are appended to the sheet named SHEET.
 * Duplicate URLs (column I) are ignored so re-runs never double up.
 */
var TOKEN = "CHANGE-ME-to-a-long-random-string";
var SHEET = "Applications";
var HEADER = ["found_date","score","verdict","title","company","location","country","source","url","status","applied_date","notes","pack_file"];

function doPost(e) {
  var body = JSON.parse(e.postData.contents || "{}");
  if (body.token !== TOKEN) return ContentService.createTextOutput("forbidden");
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET) || ss.insertSheet(SHEET);
  if (sh.getLastRow() === 0) sh.appendRow(HEADER);
  var urlCol = HEADER.indexOf("url") + 1;
  var existing = {};
  if (sh.getLastRow() > 1) {
    sh.getRange(2, urlCol, sh.getLastRow() - 1, 1).getValues().forEach(function (r) { if (r[0]) existing[r[0]] = true; });
  }
  var added = 0;
  (body.rows || []).forEach(function (row) {
    var url = row[urlCol - 1];
    if (url && !existing[url]) { sh.appendRow(row); existing[url] = true; added++; }
  });
  // status dropdown on column J for new rows
  var rule = SpreadsheetApp.newDataValidation().requireValueInList(["New","Review","Applied","Interview","Offer","Rejected","Skipped"], true).build();
  if (sh.getLastRow() > 1) sh.getRange(2, HEADER.indexOf("status") + 1, sh.getLastRow() - 1, 1).setDataValidation(rule);
  return ContentService.createTextOutput("ok added=" + added);
}

function doGet() { return ContentService.createTextOutput("job-agent tracker endpoint alive"); }
