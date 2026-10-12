# QRTS · Startup Conclave 2026 attendance

**Project maintainer: Naman Mishra**

A mobile-first Flask attendance desk for the AKGEC Startup Conclave on 15–16 October 2026. Coordinators sign in, scan an attendee QR with the browser camera, verify the attendee, select one or more checkpoints, and confirm before those Google Sheets cells are marked Present.

Repository: [namanmishra-labs/QRTS](https://github.com/namanmishra-labs/QRTS) (the repository URL supplied for this project).

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md) for inherited credential and Git history findings.

The original `main.py` sample-image decoder is preserved. `main_webcam.py` remains as a migration notice for the retired access-list flow. The web application does not call OpenCV or access a server webcam.

## Architecture

- **Flask** serves the coordinator UI and protects attendee operations behind one coordinator password. Flask sessions are HTTP-only, SameSite cookies; API mutations also require a session CSRF token.
- **Browser QR scanner:** `html5-qrcode` 2.3.8 requests the coordinator's camera only after Start Scanner is tapped. It prefers the rear camera and needs HTTPS on Android Chrome.
- **Google Sheets:** `apps_script/Code.gs` runs as the spreadsheet owner and is the only component that reads/writes Sheets. Flask sends requests server-to-server with a shared secret kept in environment variables / Apps Script Script Properties. The browser never sees it or receives the complete list except on the authenticated QR-print page.
- **Concurrency:** Apps Script `LockService` serializes writes and rechecks the current cell against the value the coordinator saw. A stale scan receives a conflict and must be scanned again. The Flask process has one Gunicorn worker in the Render blueprint; Apps Script remains the cross-request lock.
- **QR tokens:** opening Attendee QR Codes for the first time provisions missing `QRTS1:<UUID>` tokens and initializes blank checkpoint cells to `Registered`. It does not replace other spreadsheet columns. The QR list page is coordinator-authenticated and offers individual PNG downloads and print layout.

Each checkpoint has its own independent attendance cell. Values are `Registered` (not yet checked in) or `Present`. The four checkpoints are Day 1 Morning, Day 1 Evening, Day 2 Morning, and Day 2 Evening. The coordinator can mark only the selected checkpoints as `Present` after explicit confirmation. A scan alone never changes attendance; an already-present checkpoint is reported and skipped, while a different unmarked checkpoint remains eligible.

## Google Sheet setup

Create or use a spreadsheet with a header row containing these columns (the default header row is 1):

| Registration ID | Name | Phone | Email | QR Token | Day 1 Morning | Day 1 Evening | Day 2 Morning | Day 2 Evening |
|---|---|---|---|---|---|---|---|---|

Create these nine headers in row 1. The app finds columns by exact header name, so the order can vary, but all nine headers are required. Email and Phone values may be blank. Use unique Registration IDs and nonempty names. `QR Token` and all four attendance cells can initially be blank. Coordinators can edit the attendance cells in Sheets directly; use only `Registered` and `Present`. An existing seven-column sheet must be migrated to this structure before connecting it. The old two-column Morning/Evening values do not identify which individual checkpoint was attended; manually decide how to map those records to the four new columns and convert them to `Present` or `Registered`. The app does not migrate old data automatically.

1. Open [script.google.com](https://script.google.com/) while signed into the Google account that can edit the attendance spreadsheet. Create a standalone Apps Script project and paste `apps_script/Code.gs` into `Code.gs`.
2. In **Project Settings → Script Properties**, add:
   - `SPREADSHEET_ID`: the ID in the spreadsheet URL between `/d/` and `/edit`.
   - `API_SECRET`: a randomly generated secret of at least 32 bytes. For example, run `python -c "import secrets; print(secrets.token_urlsafe(48))"` locally. Keep it private.
   - `SHEET_NAME`: the exact tab name, if the spreadsheet has multiple tabs. If omitted, the first tab is used.
3. The defaults match the nine column headings above. To adjust them, add Script Properties named `HEADER_ROW`, `REGISTRATION_ID_HEADER`, `NAME_HEADER`, `EMAIL_HEADER`, `PHONE_HEADER`, `QR_TOKEN_HEADER`, `DAY1_MORNING_HEADER`, `DAY1_EVENING_HEADER`, `DAY2_MORNING_HEADER`, and `DAY2_EVENING_HEADER`. Set each value to the exact header text in your sheet. These are names, not fixed column numbers. All nine columns must be present.
4. Deploy → **New deployment** → type **Web app**. Set **Execute as** to your account. Choose **Anyone** for access so Render can reach the web app without a Google user session. The URL is internet-reachable, but every operation requires the high-entropy `API_SECRET`; the script rejects requests without it, and the secret is stored only by Apps Script and Flask/Render. Do not put it in browser code or share it. This is a shared-secret gate, not Google end-user OAuth; rotate the secret in both places if it is exposed.
5. Copy the deployed `/exec` URL. In the Apps Script editor, review the requested Google Sheets authorization and grant it to the owner account. The app does not create, replace, or clear the sheet.

The Apps Script integration has not been connected to a real sheet in this workspace. Verify it against a copy of the event sheet before relying on it at the event.

## QR email management (disabled by default)

The coordinator-only `/qr-email-test` screen supports Send to All, Send to Selected, Retry Failed, and a select-all control. Messages are sent individually with an inline QR image generated from each row's existing `QR Token`. The browser receives only masked email addresses and never receives QR tokens or full recipient addresses. The coordinator must confirm every batch. Previously sent and in-progress rows are skipped; failed rows are retried only through Retry Failed. A timeout or uncertain provider outcome remains `Sending` for manual review, which prevents an automatic duplicate or indefinite delivery retries.

Before enabling the feature, append these exact headers to the Sheet header row:

| QR Email Status | QR Email Timestamp | QR Email Error |
|---|---|---|

These are separate from the existing nine attendance/QR columns. The email tool only writes these three tracking cells. Blank status is shown as `Pending`; values used are `Sending`, `Sent`, or `Failed`. An ambiguous `Sending` record with `outcome_unknown` is shown as **Manual review** and cannot be retried until an operator checks whether the provider accepted it. Safe error codes include `quota_exhausted`, `delivery_failed`, `sender_not_authorized`, and `outcome_unknown`. Provider acceptance (`Sent`) does not prove inbox delivery. A stuck `Sending` status requires manual verification before anyone considers clearing it.

Email is delivered by Apps Script as the account that owns/runs the script; sender credentials are never added to Flask. Without an explicit sender alias, `MailApp` uses that account's sending identity. Set optional Apps Script Script Properties `SENDER_NAME` and `REPLY_TO_EMAIL` for the display name and reply address. `MailApp` does not let the script choose an arbitrary From address. To use a different From address, set `SENDER_EMAIL` to a Gmail alias that is already configured and authorized on the script owner's Google account. The script checks `GmailApp.getAliases()` and refuses to send if the requested alias is unavailable; it does not silently fall back. This path requires Gmail authorization when deploying/updating the Apps Script version. Do not use an unverified event mailbox as `SENDER_EMAIL`. The reusable server-side message identifies Startup Conclave 2026 at AKGEC, Ghaziabad, includes the attendee's name and Registration ID, and attaches that attendee's PNG QR. Google enforces per-user daily recipient quotas; check the current [MailApp documentation](https://developers.google.com/apps-script/reference/mail/mail-app), [GmailApp documentation](https://developers.google.com/apps-script/reference/gmail/gmail-app), and [Apps Script quotas](https://developers.google.com/apps-script/guides/services/quotas). Each message has one recipient and recipients never see one another.

After a coordinator signs in, `/dashboard` is the home page. QR management and the email page remain coordinator-only. There is no public sign-up route. The scanner opens over the dashboard; the coordinator reviews checkpoint selections and the verified attendee before submitting. Existing `Present` statuses are read from the server and are never written as a duplicate. For a new, different checkpoint, the coordinator can continue the update.

For testing, first set `QR_EMAIL_TEST_RECIPIENTS` to a comma-separated allow-list of inboxes you control, and ensure only approved test rows use those addresses. Then set `QR_EMAIL_TEST_MODE=true` after the tracking headers are ready. Flask sends only to allow-listed rows and caps every operation to the first three such recipients, including Send to All and Retry Failed. This mode can send real messages. To allow wider coordinator batches after testing, leave test mode `false` and explicitly set `QR_EMAIL_BULK_ENABLED=true`; both mode flags default to `false` in `.env.example` and Render. When both are false, the page and APIs are disabled. Automated tests use a fake Sheets client and never send email.

Selected rows are identified by Registration ID submitted by the browser; Flask resolves each ID against the trusted Apps Script candidate data and supplies the existing token internally. Client-submitted row numbers, email addresses, and QR tokens are not accepted. Apps Script validates the registration ID and token again, serializes sends with a script lock, and only changes the three email tracking columns.

## Local setup and run

Python 3.10+ is recommended.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with strong private values and the deployed Apps Script URL. Set `GOOGLE_APPS_SCRIPT_SECRET` to the exact same value as Script Property `API_SECRET`. For local HTTP, use `COOKIE_SECURE=false`. Load the file into the current PowerShell session (or set these variables in your IDE):

```powershell
Get-Content .env | Where-Object { $_ -match '^[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  Set-Item -Path "Env:$name" -Value $value
}
python app.py
```

Visit `http://127.0.0.1:5000`. The app listens on `PORT` (default 5000) and `0.0.0.0`, suitable for a container or Render. `/health` checks that Flask is serving; it does not contact Google.

Automated tests mock Sheets and need no Google credentials:

```powershell
python -m unittest discover -s tests -v
```

## Create and distribute attendee QR codes

After configuring the Sheet and signing into the web app, choose **Attendee QR codes**. That first authorized load creates a unique unpredictable token for each row with a Registration ID and fills each blank checkpoint cell with `Registered`. Each QR encodes only the token, never attendee contact data. Download each QR as PNG or print the full page. You may issue the same attendee code again by downloading/printing it; do not edit or reuse another attendee's `QR Token` value. QR token values are visible to spreadsheet editors, so restrict sheet access to event staff.

The inherited `Acess.txt` allow-list has been removed. Do not reuse values from that old file. Coordinator sign-in uses `COORDINATOR_PASSWORD` from the server environment and is separate from attendee QR tokens. Coordinators currently share one event password; rotate it when the authorized staff group changes.

## Deploy to Render free web service

Render currently offers a free web-service plan with 0.1 CPU / 512 MB RAM, but it spins down after 15 minutes without inbound traffic. A later request wakes it and can be slow during cold start. It is suitable for this low-volume event app, but plan for a warm-up delay and test before opening check-in. See [Render's deployment guide](https://render.com/docs/your-first-deploy), [compute plans](https://render.com/docs/compute-plans), and [free service behavior](https://render.com/docs/faq) for current limits.

1. Finish the Google Sheet and Apps Script steps above.
2. Push this repository to GitHub yourself when ready. The project changes in this working copy are not automatically pushed.
3. In Render, create a **New → Blueprint** from the repository, or create a Python web service manually. `render.yaml` defines the free plan, build command, health path, and Gunicorn start command.
4. Add environment variables in Render (the Blueprint prompts for the `sync: false` values):
   - `FLASK_SECRET_KEY`: long random secret (blueprint generates this).
   - `COORDINATOR_PASSWORD`: strong password shared only with coordinators.
   - `GOOGLE_APPS_SCRIPT_URL`: deployed Apps Script `/exec` URL.
   - `GOOGLE_APPS_SCRIPT_SECRET`: same value as Apps Script `API_SECRET`.
   - `COOKIE_SECURE=true` (Blueprint sets it; production must use HTTPS).
5. Deploy, wait for `/health` to pass, then open the `onrender.com` URL. Sign in and open the QR list to validate the sheet integration. Do a test with a designated test attendee or a copy of the sheet, then reset the test row manually.

Render's free instance is not a durable datastore. All attendance state lives in Google Sheets. Keep the sheet accessible to the Apps Script owner. The scanner library and QR generator are loaded from pinned public CDNs, so coordinators need internet access in addition to camera permission.

## Android camera check

Open the deployed HTTPS URL in Android Chrome, sign in, select the event day/checkpoint, and tap **Start scanner**. Allow camera access when Chrome asks. If denied, open the site settings using the icon next to the address, set Camera to Allow, and reload. Close other camera apps if the device reports the camera is busy. Tap Stop to release it; leaving the page also stops the camera. Camera permission and device compatibility still need verification on the actual Android phones used at the event.

## Limits and troubleshooting

- `Server setup required`: add Flask and coordinator secrets to environment variables and restart.
- `Google Sheets is not configured` / sheet errors: verify the Apps Script `/exec` URL, matching API secret, script properties, authorization, deployment version, spreadsheet ID, sheet tab, and exact header names.
- After editing Apps Script, create a new deployment version or update the existing deployment so `/exec` runs the latest code.
- `QR code was not found`: use a newly generated coordinator QR and check that the token is still present in the configured sheet.
- Conflict message: someone changed the attendee's selected checkpoint while this coordinator had the record open; scan again and review all four current statuses.
- Camera errors: camera requires HTTPS (except localhost for development), browser permission, and a free camera. The current scanner was not tested on physical Android devices here.
- Apps Script's shared-secret endpoint is internet-addressable because Render is a server without an interactive Google login. It rejects requests without the secret; protect/rotate that secret and limit spreadsheet editors. For stricter identity controls, replace this bridge with a Google Cloud service/API setup.

## Existing desktop scripts

`main.py` is the original sample-image decoder and still uses the desktop OpenCV dependencies, which are intentionally separate from the production web requirements. `main_webcam.py` is retained as a migration notice and no longer grants access from QR contents. The Flask web app is the supported coordinator workflow.

## Maintainer and inherited project notes

Naman Mishra maintains this customized QRTS project. The repository retains the original desktop prototype in Git history. The original `Acess.txt` value appeared in prior commits, so it must be considered exposed; it has been removed from the current files, but this project does not rewrite Git history. Do not use that value for any system. An authorized repository administrator must rotate/revoke it wherever it was configured and decide whether historical Git data should be purged.

The sample assets are inherited demonstration images. One QR image encodes a Shutterstock URL, and another displays Shutterstock catalog text; they are not attendee credentials or coordinator access codes. Review licensing before redistributing them. No external spreadsheet, Apps Script deployment, service account, or third-party credential is configured in this working copy.
