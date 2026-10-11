"""Flask web application for the QRTS event check-in workflow."""

import base64
import hmac
import io
import os
import re
import secrets
import threading
import time
from functools import wraps

import requests
from flask import Flask, jsonify, make_response, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix


TOKEN_PATTERN = re.compile(r"^QRTS1:[A-Za-z0-9-]{20,80}$")
ALLOWED_CHECKPOINTS = {
    "day1_morning", "day1_evening", "day2_morning", "day2_evening"
}
ALLOWED_STATUSES = {"Registered", "Present"}
QR_EMAIL_TEST_LIMIT = 3
QR_EMAIL_ERROR_MESSAGES = {
    "email_tracking_not_configured": "Add the three QR email tracking headers to the Sheet.",
    "email_test_disabled": "QR email test mode is disabled.",
    "email_not_eligible": "This row is outside the eligible test group.",
    "email_already_attempted": "An email attempt is already recorded for this attendee.",
    "email_quota_exhausted": "The Apps Script email quota is exhausted.",
    "email_delivery_failed": "The email could not be sent. Check the safe status code in the Sheet.",
    "email_invalid_image": "The generated QR image was invalid.",
    "email_confirmation_required": "Confirm one eligible attendee email.",
    "email_service_unavailable": "The QR email service is unavailable.",
    "qr_generation_failed": "The attendee QR image could not be generated.",
}
PLACEHOLDER_MARKERS = ("replace-with", "your-", "use-the-same", "change-me", "changeme", "placeholder")


def credentials_configured(secret_key, coordinator_password):
    """Reject missing, weak, or copied example credentials before serving login."""
    values = ((secret_key or "").strip(), (coordinator_password or "").strip())
    if len(values[0]) < 32 or len(values[1]) < 16:
        return False
    return not any(marker in value.lower() for value in values for marker in PLACEHOLDER_MARKERS)


class SheetsError(Exception):
    """A safe-to-report Google Sheets integration error."""


class AppsScriptSheets:
    """Server-to-server client for the secret-protected Apps Script endpoint."""

    def __init__(self, url, secret, timeout=12):
        self.url = url
        self.secret = secret
        self.timeout = timeout

    def _call(self, action, **values):
        if not self.url or not self.secret:
            raise SheetsError("Google Sheets is not configured on the server.")
        try:
            response = requests.post(
                self.url, json={"secret": self.secret, "action": action, **values},
                timeout=self.timeout, allow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SheetsError("Google Sheets could not be reached. Try again.") from exc
        if not isinstance(payload, dict):
            raise SheetsError("Google Sheets returned an invalid response.")
        if payload.get("error"):
            code = payload.get("code")
            if code == "not_found":
                return {"not_found": True}
            if code == "conflict":
                return {"conflict": True, "current_status": payload.get("current_status")}
            if isinstance(code, str) and code in QR_EMAIL_ERROR_MESSAGES:
                return {"error": True, "code": code,
                        "email_status": payload.get("email_status"),
                        "email_timestamp": payload.get("email_timestamp")}
            raise SheetsError("Google Sheets rejected the request. Check its configuration.")
        return payload

    def lookup(self, token):
        return self._call("lookup", token=token)

    def update(self, token, checkpoint, status, expected_status):
        return self._call("update", token=token, checkpoint=checkpoint, status=status,
                          expected_status=expected_status)

    def list_attendees(self):
        return self._call("list")

    def qr_email_candidates(self):
        return self._call("qr_email_candidates")

    def send_qr_email(self, row_number, qr_token, qr_png_base64):
        return self._call("send_qr_email", row_number=row_number, token=qr_token,
                          qr_png_base64=qr_png_base64)


def qr_png_base64(token):
    """Render an existing attendee token as an in-memory PNG for the email."""
    import qrcode

    image = qrcode.make(token, box_size=8, border=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def create_app(test_config=None, sheets_client=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", ""),
        COORDINATOR_PASSWORD=os.environ.get("COORDINATOR_PASSWORD", ""),
        GOOGLE_APPS_SCRIPT_URL=os.environ.get("GOOGLE_APPS_SCRIPT_URL", ""),
        GOOGLE_APPS_SCRIPT_SECRET=os.environ.get("GOOGLE_APPS_SCRIPT_SECRET", ""),
        QR_EMAIL_TEST_MODE=os.environ.get("QR_EMAIL_TEST_MODE", "false").lower() == "true",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
        MAX_CONTENT_LENGTH=8 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if app.config.get("SECRET_KEY"):
        app.secret_key = app.config["SECRET_KEY"]
    login_ready = credentials_configured(app.secret_key, app.config["COORDINATOR_PASSWORD"])
    flask_secret_key_present = bool(app.secret_key and app.secret_key.strip())
    coordinator_password_present = bool(
        app.config["COORDINATOR_PASSWORD"] and app.config["COORDINATOR_PASSWORD"].strip()
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    sheets = sheets_client or AppsScriptSheets(
        app.config["GOOGLE_APPS_SCRIPT_URL"], app.config["GOOGLE_APPS_SCRIPT_SECRET"]
    )
    update_lock = threading.Lock()
    login_lock = threading.Lock()
    failed_logins = {}

    def authenticated(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("coordinator"):
                return jsonify(error="Authentication required.", code="unauthorized"), 401
            supplied = request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                return jsonify(error="Request verification failed. Reload the page."), 403
            return view(*args, **kwargs)
        return wrapped

    @app.after_request
    def secure_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' https://unpkg.com https://cdnjs.cloudflare.com; "
            "worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.get("/")
    def index():
        if not login_ready:
            return render_template("setup.html"), 503
        if session.get("coordinator"):
            return render_template("checkin.html", csrf_token=session["csrf_token"],
                                   qr_email_test_mode=app.config["QR_EMAIL_TEST_MODE"])
        return render_template("login.html")

    @app.post("/login")
    def login():
        if not login_ready:
            return render_template("setup.html"), 503
        password = request.form.get("password", "")
        client_key = request.remote_addr or "unknown"
        now = time.monotonic()
        with login_lock:
            attempts = [stamp for stamp in failed_logins.get(client_key, []) if now - stamp < 900]
            failed_logins[client_key] = attempts
            if len(attempts) >= 5:
                return render_template("login.html", error="Too many attempts. Wait 15 minutes and try again."), 429
        if not hmac.compare_digest(password, app.config["COORDINATOR_PASSWORD"]):
            with login_lock:
                failed_logins.setdefault(client_key, []).append(now)
            return render_template("login.html", error="Incorrect coordinator password."), 401
        with login_lock:
            failed_logins.pop(client_key, None)
        session.clear()
        session["coordinator"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        return make_response("", 303, {"Location": "/"})

    @app.post("/logout")
    def logout():
        if session.get("coordinator"):
            supplied = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(supplied, session.get("csrf_token", "")):
                return "Request verification failed.", 403
        session.clear()
        return make_response("", 303, {"Location": "/"})

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/health/config")
    def config_health():
        """Expose only boolean presence/readiness signals, never configuration values."""
        return jsonify(
            flask_secret_key_present=flask_secret_key_present,
            coordinator_password_present=coordinator_password_present,
            setup_ready=login_ready,
        )

    @app.get("/qrcodes")
    def qrcodes():
        if not session.get("coordinator"):
            return make_response("", 303, {"Location": "/"})
        return render_template("qrcodes.html", csrf_token=session["csrf_token"])

    @app.get("/qr-email-test")
    def qr_email_test_page():
        if not session.get("coordinator"):
            return make_response("", 303, {"Location": "/"})
        if not app.config["QR_EMAIL_TEST_MODE"]:
            return make_response("QR email test mode is disabled.", 404)
        return render_template("qr_email_test.html", csrf_token=session["csrf_token"])

    def qr_email_error(result, default_status=502):
        code = result.get("code") if isinstance(result, dict) else None
        if not isinstance(code, str):
            code = None
        message = QR_EMAIL_ERROR_MESSAGES.get(code, "The QR email request could not be completed.")
        status = 503 if code == "email_tracking_not_configured" else default_status
        if code == "email_quota_exhausted":
            status = 429
        elif code == "email_already_attempted":
            status = 409
        elif code == "email_not_eligible":
            status = 404
        elif code == "email_invalid_image":
            status = 400
        return jsonify(error=message, code=code or "email_error"), status

    @app.post("/api/qr-email/candidates")
    @authenticated
    def qr_email_candidates():
        if not app.config["QR_EMAIL_TEST_MODE"]:
            return jsonify(error=QR_EMAIL_ERROR_MESSAGES["email_test_disabled"],
                           code="email_test_disabled"), 404
        try:
            result = sheets.qr_email_candidates()
        except SheetsError:
            return jsonify(error="QR email candidates could not be loaded.",
                           code="email_service_unavailable"), 502
        if result.get("error"):
            return qr_email_error(result)
        candidates = result.get("candidates", [])
        # Defense in depth: never expose or offer more than the fixed test cohort.
        safe_candidates = []
        for item in candidates[:QR_EMAIL_TEST_LIMIT]:
            status = item.get("email_status") or "Not sent"
            if status not in {"Not sent", "Sending", "Sent", "Failed"}:
                status = "Unknown"
            error = item.get("email_error", "")
            if error not in {"", "quota_exhausted", "delivery_failed"}:
                error = ""
            timestamp = item.get("email_timestamp", "")
            if not isinstance(timestamp, str) or len(timestamp) > 40:
                timestamp = ""
            safe_candidates.append({
                "row_number": item.get("row_number"),
                "registration_id": item.get("registration_id", ""),
                "name": item.get("name", ""),
                "email_status": status,
                "email_timestamp": timestamp,
                "email_error": error,
            })
        return jsonify(candidates=safe_candidates)

    @app.post("/api/qr-email/send")
    @authenticated
    def send_qr_email():
        if not app.config["QR_EMAIL_TEST_MODE"]:
            return jsonify(error=QR_EMAIL_ERROR_MESSAGES["email_test_disabled"],
                           code="email_test_disabled"), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Invalid request.", code="invalid_request"), 400
        row_number = body.get("row_number")
        if (not isinstance(row_number, int) or isinstance(row_number, bool) or
                row_number < 2 or body.get("confirm_send") is not True):
            return jsonify(error="Confirm one eligible attendee email.",
                           code="email_confirmation_required"), 400
        try:
            result = sheets.qr_email_candidates()
        except SheetsError:
            return jsonify(error="QR email candidates could not be loaded.",
                           code="email_service_unavailable"), 502
        if result.get("error"):
            return qr_email_error(result)
        candidates = result.get("candidates", [])[:QR_EMAIL_TEST_LIMIT]
        candidate = next((item for item in candidates
                          if item.get("row_number") == row_number), None)
        if not candidate:
            return qr_email_error({"code": "email_not_eligible"}, 404)
        if candidate.get("email_status"):
            return qr_email_error({"code": "email_already_attempted"}, 409)
        token = candidate.get("qr_token", "")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return qr_email_error({"code": "email_not_eligible"}, 400)
        try:
            image_data = qr_png_base64(token)
        except Exception:
            return jsonify(error="The attendee QR image could not be generated.",
                           code="qr_generation_failed"), 500
        try:
            result = sheets.send_qr_email(row_number, token, image_data)
        except SheetsError:
            return jsonify(error="The QR email service could not be reached.",
                           code="email_service_unavailable"), 502
        if result.get("error"):
            return qr_email_error(result)
        return jsonify(message="The individual QR email was sent.",
                       email_status=result.get("email_status", "Sent"),
                       email_timestamp=result.get("email_timestamp", ""))

    @app.post("/api/lookup")
    @authenticated
    def lookup():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="Invalid request.", code="invalid_request"), 400
        token = body.get("token", "")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return jsonify(error="This QR code is not valid.", code="invalid_qr"), 400
        try:
            result = sheets.lookup(token)
        except SheetsError as exc:
            return jsonify(error=str(exc), code="sheets_error"), 502
        if result.get("not_found") or not result.get("attendee"):
            return jsonify(error="QR code was not found in this event.", code="unknown_qr"), 404
        return jsonify(attendee=result["attendee"])

    @app.post("/api/attendance")
    @authenticated
    def attendance():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="Invalid request.", code="invalid_request"), 400
        token = body.get("token")
        checkpoint, status = body.get("checkpoint"), body.get("status")
        expected_status = body.get("expected_status")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return jsonify(error="This QR code is not valid.", code="invalid_qr"), 400
        if not isinstance(checkpoint, str) or checkpoint not in ALLOWED_CHECKPOINTS:
            return jsonify(error="Choose a valid attendance checkpoint.", code="invalid_checkpoint"), 400
        if (not isinstance(status, str) or status not in ALLOWED_STATUSES or
                not isinstance(expected_status, str) or expected_status not in ALLOWED_STATUSES):
            return jsonify(error="Choose a valid attendance status.", code="invalid_status"), 400
        if status == expected_status:
            return jsonify(error="The attendee already has this status.", code="duplicate",
                           status=status), 200
        if expected_status != "Registered" and body.get("confirm_status_change") is not True:
            return jsonify(error="Confirm the change to the existing attendance status.",
                           code="status_change_confirmation_required",
                           current_status=expected_status), 428
        try:
            # This protects concurrent requests handled by a single app worker.
            # Apps Script also uses LockService and checks expected_status atomically.
            with update_lock:
                result = sheets.update(token, checkpoint, status, expected_status)
        except SheetsError as exc:
            return jsonify(error=str(exc), code="sheets_error"), 502
        if result.get("not_found"):
            return jsonify(error="QR code was not found in this event.", code="unknown_qr"), 404
        if result.get("conflict"):
            return jsonify(error="Attendance changed since it was loaded. Scan again.",
                           code="conflict", current_status=result.get("current_status")), 409
        return jsonify(message="Attendance updated.", status=status, duplicate=False)

    @app.post("/api/attendees/qr")
    def attendee_qr_list():
        if not session.get("coordinator"):
            return jsonify(error="Authentication required."), 401
        supplied = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(supplied, session.get("csrf_token", "")):
            return jsonify(error="Request verification failed."), 403
        try:
            result = sheets.list_attendees()
        except SheetsError as exc:
            return jsonify(error=str(exc)), 502
        return jsonify(attendees=result.get("attendees", []))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
