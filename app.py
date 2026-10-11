"""Flask web application for the QRTS event check-in workflow."""

import hmac
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
PLACEHOLDER_MARKERS = ("replace-with", "your-", "use-the-same", "change-me", "changeme", "placeholder")


def credentials_configured(secret_key, coordinator_password):
    """Reject missing, weak, or copied example credentials before serving login."""
    values = (secret_key or "", coordinator_password or "")
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
            raise SheetsError("Google Sheets rejected the request. Check its configuration.")
        return payload

    def lookup(self, token):
        return self._call("lookup", token=token)

    def update(self, token, checkpoint, status, expected_status):
        return self._call("update", token=token, checkpoint=checkpoint, status=status,
                          expected_status=expected_status)

    def list_attendees(self):
        return self._call("list")


def create_app(test_config=None, sheets_client=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", ""),
        COORDINATOR_PASSWORD=os.environ.get("COORDINATOR_PASSWORD", ""),
        GOOGLE_APPS_SCRIPT_URL=os.environ.get("GOOGLE_APPS_SCRIPT_URL", ""),
        GOOGLE_APPS_SCRIPT_SECRET=os.environ.get("GOOGLE_APPS_SCRIPT_SECRET", ""),
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
            return render_template("checkin.html", csrf_token=session["csrf_token"])
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

    @app.get("/qrcodes")
    def qrcodes():
        if not session.get("coordinator"):
            return make_response("", 303, {"Location": "/"})
        return render_template("qrcodes.html", csrf_token=session["csrf_token"])

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
