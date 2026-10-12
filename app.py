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
CHECKPOINT_ORDER = ("day1_morning", "day1_evening", "day2_morning", "day2_evening")
ALLOWED_CHECKPOINTS = set(CHECKPOINT_ORDER)
ALLOWED_STATUSES = {"Registered", "Present"}
QR_EMAIL_TEST_LIMIT = 3
QR_EMAIL_BATCH_LIMIT = 3
QR_EMAIL_ERROR_MESSAGES = {
    "email_tracking_not_configured": "Add the three QR email tracking headers to the Sheet.",
    "email_test_disabled": "QR email test mode is disabled.",
    "email_not_eligible": "This row is outside the eligible test group.",
    "email_already_attempted": "An email attempt is already recorded for this attendee.",
    "email_quota_exhausted": "The Apps Script email quota is exhausted.",
    "email_delivery_failed": "The email could not be sent. Check the safe status code in the Sheet.",
    "email_invalid_image": "The generated QR image was invalid.",
    "email_confirmation_required": "Confirm one eligible attendee email.",
    "email_invalid_recipient": "This attendee has no valid email address.",
    "email_sender_not_authorized": "The configured sender is not an authorized account or alias. No email was sent.",
    "email_invalid_selection": "Choose attendees from the current list.",
    "email_retry_not_allowed": "Only one delivery retry is allowed. Review the email status before taking further action.",
    "email_ambiguous_outcome": "The send outcome needs manual review before retrying.",
    "email_in_progress": "An email operation is already running. Reload the status before retrying.",
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

    def _call(self, action, timeout=None, **values):
        if not self.url or not self.secret:
            raise SheetsError("Google Sheets is not configured on the server.")
        try:
            response = requests.post(
                self.url, json={"secret": self.secret, "action": action, **values},
                timeout=self.timeout if timeout is None else timeout, allow_redirects=True,
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
        return self._call("qr_email_candidates", timeout=6)

    def send_qr_email(self, registration_id, qr_token, qr_png_base64, retry_failed=False):
        return self._call("send_qr_email", timeout=6, registration_id=registration_id, token=qr_token,
                          qr_png_base64=qr_png_base64, retry_failed=retry_failed)


def qr_png_base64(token):
    """Render an existing attendee token as an in-memory PNG for the email."""
    import qrcode

    image = qrcode.make(token, box_size=8, border=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def mask_email(value):
    """Return a display-safe email hint without exposing the local part."""
    if not isinstance(value, str) or "@" not in value:
        return "Invalid or missing"
    _local, domain = value.rsplit("@", 1)
    if not domain or "." not in domain:
        return "Invalid or missing"
    return f"***@{domain}"


def run_qr_email_batch(sheets, body, config, error_messages, error_response,
                       qr_factory, test_limit):
    """Validate trusted sheet candidates and deliver one message per attendee."""
    if not (config.get("QR_EMAIL_TEST_MODE") or config.get("QR_EMAIL_BULK_ENABLED")):
        return jsonify(error=error_messages["email_test_disabled"],
                       code="email_test_disabled"), 404
    if not isinstance(body, dict) or body.get("confirm_send") is not True:
        return jsonify(error=error_messages["email_confirmation_required"],
                       code="email_confirmation_required"), 400
    action = body.get("action")
    if action not in {"all", "selected", "retry_failed"}:
        return jsonify(error="Choose a valid email action.", code="invalid_action"), 400
    selected_ids = body.get("registration_ids", [])
    if action == "selected" and (not isinstance(selected_ids, list) or not selected_ids or
            len(selected_ids) > QR_EMAIL_BATCH_LIMIT or any(not isinstance(value, str) or
            not 1 <= len(value.strip()) <= 80 for value in selected_ids) or
            len({value.strip() for value in selected_ids}) != len(selected_ids)):
        return jsonify(error=error_messages["email_invalid_selection"],
                       code="email_invalid_selection"), 400
    try:
        result = sheets.qr_email_candidates()
    except SheetsError:
        return jsonify(error="QR email candidates could not be loaded.",
                       code="email_service_unavailable"), 502
    if result.get("error"):
        return error_response(result)
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        return jsonify(error="QR email candidates could not be loaded.",
                       code="email_service_unavailable"), 502
    candidates = [item for item in candidates if isinstance(item, dict)]
    if config.get("QR_EMAIL_TEST_MODE"):
        allowed = config.get("QR_EMAIL_TEST_RECIPIENTS", set())
        candidates = [item for item in candidates if item.get("email_valid") is True and
                      str(item.get("email", "")).strip().lower() in allowed][:test_limit]

    by_id = {}
    duplicate_ids = set()
    for candidate in candidates:
        registration_id = candidate.get("registration_id")
        if not isinstance(registration_id, str) or not registration_id.strip():
            continue
        if registration_id in by_id:
            duplicate_ids.add(registration_id)
        else:
            by_id[registration_id] = candidate
    if action == "selected":
        normalized = [value.strip() for value in selected_ids]
        if any(value not in by_id or value in duplicate_ids for value in normalized):
            return jsonify(error=error_messages["email_invalid_selection"],
                           code="email_invalid_selection"), 400
        requested = [by_id[value] for value in normalized]
    elif action == "retry_failed":
        requested = [item for item in candidates if item.get("email_status") == "Failed"]
    else:
        requested = list(candidates)

    remaining_count = max(0, len(requested) - QR_EMAIL_BATCH_LIMIT)
    if action in {"all", "retry_failed"}:
        requested = requested[:QR_EMAIL_BATCH_LIMIT]
    selected_count = len(requested)
    sent_count = failed_count = skipped_count = manual_review_count = processed_count = eligible_count = 0
    outcomes = []
    for candidate in requested:
        reg_id = candidate.get("registration_id", "")
        status = candidate.get("email_status") or "Pending"
        if ((status == "Sending" and candidate.get("email_error") == "outcome_unknown") or
                (status == "Failed" and candidate.get("email_error") == "retry_exhausted")):
            status = "Manual review"
        if reg_id in duplicate_ids or not candidate.get("email_valid"):
            skipped_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Skipped",
                             "reason": "invalid_email" if not candidate.get("email_valid") else "duplicate_registration"})
            continue
        if status == "Manual review":
            manual_review_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Review",
                             "reason": "retry_limit_reached" if candidate.get("email_error") == "retry_exhausted"
                             else "outcome_unknown"})
            continue
        if status == "Sent" or status == "Sending" or status not in {"Pending", "Failed"}:
            skipped_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Skipped",
                             "reason": "already_sent" if status == "Sent" else "attempt_in_progress"})
            continue
        if action == "retry_failed" and status != "Failed":
            skipped_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Skipped", "reason": "not_failed"})
            continue
        token = candidate.get("qr_token", "")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            skipped_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Skipped", "reason": "invalid_qr"})
            continue
        eligible_count += 1
        processed_count += 1
        try:
            image_data = qr_factory(token)
        except Exception:
            failed_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Failed", "reason": "qr_generation_failed"})
            continue
        try:
            send_result = sheets.send_qr_email(
                reg_id, token, image_data,
                retry_failed=(status == "Failed"),
            )
        except SheetsError:
            # A network timeout can happen after provider acceptance. Do not retry
            # automatically; keep this outcome distinct for coordinator review.
            manual_review_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Review", "reason": "outcome_unknown"})
            continue
        if send_result.get("sent") is True and send_result.get("email_status") == "Sent":
            sent_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Sent"})
        elif send_result.get("error"):
            if send_result.get("code") == "email_already_attempted":
                skipped_count += 1
                outcomes.append({"registration_id": reg_id, "status": "Skipped", "reason": "already_attempted"})
            elif send_result.get("code") == "email_in_progress":
                skipped_count += 1
                outcomes.append({"registration_id": reg_id, "status": "Skipped", "reason": "attempt_in_progress"})
            elif send_result.get("code") == "email_invalid_recipient":
                skipped_count += 1
                outcomes.append({"registration_id": reg_id, "status": "Skipped", "reason": "invalid_email"})
            elif send_result.get("code") == "email_ambiguous_outcome":
                manual_review_count += 1
                outcomes.append({"registration_id": reg_id, "status": "Review", "reason": "outcome_unknown"})
            else:
                failed_count += 1
                code = send_result.get("code")
                safe_code = code if code in {"email_quota_exhausted", "email_delivery_failed", "email_sender_not_authorized", "qr_generation_failed"} else "email_delivery_failed"
                outcomes.append({"registration_id": reg_id, "status": "Failed", "reason": safe_code})
        else:
            manual_review_count += 1
            outcomes.append({"registration_id": reg_id, "status": "Review", "reason": "outcome_unknown"})
    return jsonify(action=action, selected_count=selected_count, eligible_count=eligible_count,
                   processed_count=processed_count,
                   sent_count=sent_count, failed_count=failed_count,
                   skipped_count=skipped_count, manual_review_count=manual_review_count,
                   remaining_count=remaining_count,
                   outcomes=outcomes), 200


def create_app(test_config=None, sheets_client=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", ""),
        COORDINATOR_PASSWORD=os.environ.get("COORDINATOR_PASSWORD", ""),
        GOOGLE_APPS_SCRIPT_URL=os.environ.get("GOOGLE_APPS_SCRIPT_URL", ""),
        GOOGLE_APPS_SCRIPT_SECRET=os.environ.get("GOOGLE_APPS_SCRIPT_SECRET", ""),
        QR_EMAIL_TEST_MODE=os.environ.get("QR_EMAIL_TEST_MODE", "false").lower() == "true",
        QR_EMAIL_BULK_ENABLED=os.environ.get("QR_EMAIL_BULK_ENABLED", "false").lower() == "true",
        QR_EMAIL_TEST_RECIPIENTS=os.environ.get("QR_EMAIL_TEST_RECIPIENTS", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
        MAX_CONTENT_LENGTH=8 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    configured_test_recipients = app.config.get("QR_EMAIL_TEST_RECIPIENTS", "")
    if isinstance(configured_test_recipients, str):
        configured_test_recipients = configured_test_recipients.split(",")
    app.config["QR_EMAIL_TEST_RECIPIENTS"] = {
        value.strip().lower() for value in configured_test_recipients
        if isinstance(value, str) and value.strip()
    }
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
            return make_response("", 303, {"Location": "/dashboard"})
        return render_template("login.html")

    @app.get("/dashboard")
    def dashboard():
        if not login_ready:
            return render_template("setup.html"), 503
        if not session.get("coordinator"):
            return make_response("", 303, {"Location": "/"})
        return render_template("checkin.html", csrf_token=session["csrf_token"],
                               qr_email_test_mode=app.config["QR_EMAIL_TEST_MODE"],
                               qr_email_bulk_enabled=app.config["QR_EMAIL_BULK_ENABLED"])

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
        return make_response("", 303, {"Location": "/dashboard"})

    @app.post("/logout")
    def logout():
        if session.get("coordinator"):
            supplied = request.headers.get("X-CSRF-Token", "") or request.form.get("_csrf", "")
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
        return render_template("qrcodes.html", csrf_token=session["csrf_token"],
                               qr_email_test_mode=app.config["QR_EMAIL_TEST_MODE"],
                               qr_email_bulk_enabled=app.config["QR_EMAIL_BULK_ENABLED"])

    @app.get("/qr-email-test")
    def qr_email_test_page():
        if not session.get("coordinator"):
            return make_response("", 303, {"Location": "/"})
        if not (app.config["QR_EMAIL_TEST_MODE"] or app.config["QR_EMAIL_BULK_ENABLED"]):
            return make_response("QR email test mode is disabled.", 404)
        return render_template("qr_email_test.html", csrf_token=session["csrf_token"],
                               test_mode=app.config["QR_EMAIL_TEST_MODE"],
                               bulk_enabled=app.config["QR_EMAIL_BULK_ENABLED"])

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
        elif code in {"email_invalid_selection", "email_invalid_recipient", "email_retry_not_allowed"}:
            status = 400
        return jsonify(error=message, code=code or "email_error"), status

    @app.post("/api/qr-email/candidates")
    @authenticated
    def qr_email_candidates():
        if not (app.config["QR_EMAIL_TEST_MODE"] or app.config["QR_EMAIL_BULK_ENABLED"]):
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
        if not isinstance(candidates, list):
            return jsonify(error="QR email candidates could not be loaded.",
                           code="email_service_unavailable"), 502
        candidates = [item for item in candidates if isinstance(item, dict)]
        # Test mode is always bounded to the first three valid email recipients.
        if app.config["QR_EMAIL_TEST_MODE"]:
            allowed = app.config["QR_EMAIL_TEST_RECIPIENTS"]
            candidates = [item for item in candidates if item.get("email_valid") is True and
                          str(item.get("email", "")).strip().lower() in allowed][:QR_EMAIL_TEST_LIMIT]
        safe_candidates = []
        for item in candidates:
            status = item.get("email_status") or "Pending"
            if ((status == "Sending" and item.get("email_error") == "outcome_unknown") or
                    (status == "Failed" and item.get("email_error") == "retry_exhausted")):
                status = "Manual review"
            if status not in {"Pending", "Sending", "Sent", "Failed", "Manual review"}:
                status = "Unknown"
            error = item.get("email_error", "")
            if error not in {"", "quota_exhausted", "delivery_failed", "outcome_unknown",
                             "sender_not_authorized", "retry_exhausted"}:
                error = ""
            timestamp = item.get("email_timestamp", "")
            if not isinstance(timestamp, str) or len(timestamp) > 40:
                timestamp = ""
            safe_candidates.append({
                "registration_id": item.get("registration_id", ""),
                "name": item.get("name", ""),
                "email": mask_email(item.get("email", "")) if item.get("email_valid") else "Invalid or missing",
                "email_valid": item.get("email_valid") is True,
                "email_status": status,
                "email_timestamp": timestamp,
                "email_error": error,
            })
        return jsonify(candidates=safe_candidates)

    @app.post("/api/qr-email/send")
    @authenticated
    def send_qr_email():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("registration_id"), str):
            return jsonify(error=QR_EMAIL_ERROR_MESSAGES["email_confirmation_required"],
                           code="email_confirmation_required"), 400
        single = {"action": "selected", "registration_ids": [body["registration_id"]],
                  "confirm_send": body.get("confirm_send")}
        return run_qr_email_batch(sheets, single, app.config,
                                  QR_EMAIL_ERROR_MESSAGES, qr_email_error,
                                  qr_png_base64, QR_EMAIL_TEST_LIMIT)

    @app.post("/api/qr-email/batch")
    @authenticated
    def send_qr_email_batch():
        return run_qr_email_batch(sheets, request.get_json(silent=True), app.config,
                                  QR_EMAIL_ERROR_MESSAGES, qr_email_error,
                                  qr_png_base64, QR_EMAIL_TEST_LIMIT)

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
        attendee = result["attendee"]
        already_present = [key for key in CHECKPOINT_ORDER
                           if attendee.get(key) == "Present"]
        return jsonify(attendee=attendee, already_present_checkpoints=already_present)

    @app.post("/api/attendance")
    @authenticated
    def attendance():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="Invalid request.", code="invalid_request"), 400
        token = body.get("token")
        if "checkpoints" in body:
            if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
                return jsonify(error="This QR code is not valid.", code="invalid_qr"), 400
            selections = body.get("checkpoints")
            if not isinstance(selections, list) or not 1 <= len(selections) <= len(ALLOWED_CHECKPOINTS):
                return jsonify(error="Select one or more valid checkpoints.", code="invalid_checkpoints"), 400
            requested = []
            seen = set()
            for selection in selections:
                if not isinstance(selection, dict):
                    return jsonify(error="Invalid checkpoint selection.", code="invalid_checkpoints"), 400
                checkpoint_key = selection.get("checkpoint")
                expected = selection.get("expected_status")
                if (not isinstance(checkpoint_key, str) or checkpoint_key not in ALLOWED_CHECKPOINTS or
                        checkpoint_key in seen or not isinstance(expected, str) or
                        expected not in ALLOWED_STATUSES):
                    return jsonify(error="Invalid or duplicate checkpoint selection.",
                                   code="invalid_checkpoints"), 400
                requested.append((checkpoint_key, expected))
                seen.add(checkpoint_key)

            updated = []
            already_present = []
            failure = None
            http_status = 409
            try:
                # Apps Script checks each expected status atomically under its own lock.
                # The local lock also prevents this app worker from interleaving its batch.
                with update_lock:
                    current_result = sheets.lookup(token)
                    if current_result.get("not_found") or not current_result.get("attendee"):
                        return jsonify(error="QR code was not found in this event.",
                                       code="unknown_qr", updated_checkpoints=[],
                                       already_present=[]), 404
                    attendee = current_result["attendee"]
                    for checkpoint_key, expected in requested:
                        actual = attendee.get(checkpoint_key, "Registered")
                        if actual not in ALLOWED_STATUSES:
                            failure = (checkpoint_key, "invalid_current_status",
                                       "A selected checkpoint has an unsupported status.", None)
                            http_status = 502
                            break
                        if actual == "Present":
                            already_present.append(checkpoint_key)
                            continue
                        if actual != expected:
                            failure = (checkpoint_key, "conflict",
                                       "Attendance changed since it was loaded. Review and retry.", actual)
                            http_status = 409
                            break
                        result = sheets.update(token, checkpoint_key, "Present", expected)
                        if result.get("not_found"):
                            failure = (checkpoint_key, "unknown_qr",
                                       "QR code was not found in this event.", None)
                            http_status = 404
                            break
                        if result.get("conflict"):
                            if result.get("current_status") == "Present":
                                already_present.append(checkpoint_key)
                                continue
                            failure = (checkpoint_key, "conflict",
                                       "Attendance changed since it was loaded. Review and retry.",
                                       result.get("current_status"))
                            http_status = 409
                            break
                        if result.get("duplicate"):
                            already_present.append(checkpoint_key)
                        elif result.get("updated"):
                            updated.append(checkpoint_key)
                        else:
                            failure = (checkpoint_key, "sheets_error",
                                       "Google Sheets returned an incomplete checkpoint result.", None)
                            http_status = 502
                            break
            except SheetsError:
                completed_keys = set(updated) | set(already_present)
                failed_checkpoint = next(
                    (key for key, _expected in requested if key not in completed_keys), None
                )
                failure = (failed_checkpoint, "sheets_error",
                           "Google Sheets could not complete this checkpoint update. Retry safely.", None)
                http_status = 502

            if failure:
                failed_checkpoint, code, error, current_status = failure
                partial = bool(updated or already_present)
                return jsonify(success=False, error=error, code=code, partial=partial,
                               updated_checkpoints=updated, already_present=already_present,
                               failed_checkpoint=failed_checkpoint,
                               current_status=current_status), http_status
            return jsonify(success=True, updated_checkpoints=updated,
                           already_present=already_present, failed_checkpoint=None)

        checkpoint, status = body.get("checkpoint"), body.get("status")
        expected_status = body.get("expected_status")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return jsonify(error="This QR code is not valid.", code="invalid_qr"), 400
        if not isinstance(checkpoint, str) or checkpoint not in ALLOWED_CHECKPOINTS:
            return jsonify(error="Choose a valid attendance checkpoint.", code="invalid_checkpoint"), 400
        if (not isinstance(status, str) or status not in ALLOWED_STATUSES or
                not isinstance(expected_status, str) or expected_status not in ALLOWED_STATUSES):
            return jsonify(error="Choose a valid attendance status.", code="invalid_status"), 400
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
        duplicate = bool(result.get("duplicate"))
        return jsonify(message="Attendance already has this status." if duplicate else "Attendance updated.",
                       status=result.get("status", status), duplicate=duplicate,
                       code="duplicate" if duplicate else "updated")

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
