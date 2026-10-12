import threading
import unittest
from pathlib import Path

from unittest.mock import Mock, patch

from app import AppsScriptSheets, SheetsError, create_app


class FakeSheets:
    def __init__(self):
        self.attendees = {
            "QRTS1:0123456789abcdef0123456789abcdef": {
                "registration_id": "REG-1", "name": "A Student",
                "day1_morning": "Registered", "day1_evening": "Registered",
                "day2_morning": "Registered", "day2_evening": "Registered",
            }
        }
        self.lock = threading.Lock()
        self.fail = False
        self.fail_checkpoint = None

    def lookup(self, token):
        if self.fail:
            raise SheetsError("Google Sheets could not be reached. Try again.")
        attendee = self.attendees.get(token)
        return {"attendee": dict(attendee)} if attendee else {"not_found": True}

    def update(self, token, day, status, expected_status):
        if self.fail:
            raise SheetsError("Google Sheets could not be reached. Try again.")
        if self.fail_checkpoint == day:
            raise SheetsError("Google Sheets could not be reached. Try again.")
        with self.lock:
            attendee = self.attendees.get(token)
            if attendee is None:
                return {"not_found": True}
            if attendee[day] != expected_status:
                return {"conflict": True, "current_status": attendee[day]}
            if attendee[day] == status:
                return {"duplicate": True, "status": status}
            attendee[day] = status
            return {"updated": True, "status": status}

    def list_attendees(self):
        return {"attendees": []}

    def _qr_email_rows(self):
        if not hasattr(self, "email_rows"):
            self.email_rows = [
                {"row_number": 2, "registration_id": "TEST-1", "name": "Test Attendee 1",
                 "email": "attendee1@example.test", "qr_token": "QRTS1:00000000-0000-4000-8000-000000000001",
                 "email_valid": True,
                 "email_status": "", "email_timestamp": "", "email_error": ""},
                {"row_number": 3, "registration_id": "TEST-2", "name": "Test Attendee 2",
                 "email": "attendee2@example.test", "qr_token": "QRTS1:00000000-0000-4000-8000-000000000002",
                 "email_valid": True,
                 "email_status": "", "email_timestamp": "", "email_error": ""},
                {"row_number": 4, "registration_id": "TEST-3", "name": "Test Attendee 3",
                 "email": "attendee3@example.test", "qr_token": "QRTS1:00000000-0000-4000-8000-000000000003",
                 "email_valid": True,
                 "email_status": "", "email_timestamp": "", "email_error": ""},
                {"row_number": 5, "registration_id": "TEST-4", "name": "Outside Test Cohort",
                 "email": "outside@example.test", "qr_token": "QRTS1:00000000-0000-4000-8000-000000000004",
                 "email_valid": True,
                 "email_status": "", "email_timestamp": "", "email_error": ""},
            ]
        return self.email_rows

    def qr_email_candidates(self):
        return {"candidates": [dict(row) for row in self._qr_email_rows()]}

    def send_qr_email(self, registration_id, qr_token, qr_png_base64, retry_failed=False):
        row = next((item for item in self._qr_email_rows()
                    if item["registration_id"] == registration_id), None)
        if row is None:
            return {"error": True, "code": "email_not_eligible"}
        if row["email_status"] == "Sent" or row["email_status"] == "Sending":
            return {"error": True, "code": "email_already_attempted"}
        if row["email_status"] == "Failed" and not retry_failed:
            return {"error": True, "code": "email_already_attempted"}
        if registration_id in getattr(self, "fail_email_ids", set()):
            row["email_status"] = "Failed"
            row["email_error"] = "delivery_failed"
            return {"error": True, "code": "email_delivery_failed", "email_status": "Failed"}
        self.email_sends = getattr(self, "email_sends", [])
        self.email_sends.append({"registration_id": registration_id, "qr_token": qr_token,
                                 "qr_png_base64": qr_png_base64})
        row["email_status"] = "Sent"
        row["email_timestamp"] = "test timestamp"
        return {"sent": True, "email_status": "Sent", "email_timestamp": row["email_timestamp"]}


class AttendanceAppTests(unittest.TestCase):
    token = "QRTS1:0123456789abcdef0123456789abcdef"

    def setUp(self):
        self.sheets = FakeSheets()
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-secret-key-that-is-at-least-32-characters-long",
            "COORDINATOR_PASSWORD": "coordinator-pass",
            "SESSION_COOKIE_SECURE": False,
        }, self.sheets)
        self.app.config["QR_EMAIL_TEST_MODE"] = True
        self.app.config["QR_EMAIL_TEST_RECIPIENTS"] = {
            "attendee1@example.test", "attendee2@example.test", "attendee3@example.test"
        }
        self.client = self.app.test_client()
        self.csrf = ""

    def login(self):
        response = self.client.post("/login", data={"password": "coordinator-pass"})
        self.assertEqual(response.status_code, 303)
        with self.client.session_transaction() as state:
            self.csrf = state["csrf_token"]

    def post_json(self, path, data):
        return self.client.post(path, json=data, headers={"X-CSRF-Token": self.csrf})

    def test_coordinator_authentication_and_csrf(self):
        self.assertEqual(self.client.post("/api/lookup", json={"token": self.token}).status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/lookup", json={"token": self.token}).status_code, 403)

    def test_login_throttles_repeated_failed_attempts(self):
        for _ in range(5):
            response = self.client.post("/login", data={"password": "wrong"})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post("/login", data={"password": "wrong"}).status_code, 429)

    def test_valid_lookup_and_invalid_tokens(self):
        self.login()
        response = self.post_json("/api/lookup", {"token": self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["attendee"]["name"], "A Student")
        invalid = self.post_json("/api/lookup", {"token": "0000"})
        self.assertEqual(invalid.status_code, 400)
        unknown = self.post_json("/api/lookup", {"token": "QRTS1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
        self.assertEqual(unknown.status_code, 404)

    def test_updates_for_all_four_independent_checkpoints_and_duplicate(self):
        self.login()
        checkpoints = ("day1_morning", "day1_evening", "day2_morning", "day2_evening")
        for checkpoint in checkpoints:
            response = self.post_json("/api/attendance", {
                "token": self.token, "checkpoint": checkpoint, "status": "Present",
                "expected_status": "Registered",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.sheets.attendees[self.token][checkpoint], "Present")
            for other_checkpoint in checkpoints:
                if other_checkpoint != checkpoint:
                    self.assertEqual(self.sheets.attendees[self.token][other_checkpoint], "Registered")
            repeated = self.post_json("/api/attendance", {
                "token": self.token, "checkpoint": checkpoint, "status": "Present",
                "expected_status": "Present",
            })
            self.assertEqual(repeated.json["code"], "duplicate")
            registered = self.post_json("/api/attendance", {
                "token": self.token, "checkpoint": checkpoint, "status": "Registered",
                "expected_status": "Present",
            })
            self.assertEqual(registered.status_code, 200)
            self.assertEqual(self.sheets.attendees[self.token][checkpoint], "Registered")
        self.assertEqual(set(self.sheets.attendees[self.token].values()), {"REG-1", "A Student", "Registered"})

    def test_first_time_scan_shows_unmarked_day_and_update_requires_explicit_action(self):
        self.login()
        lookup = self.post_json("/api/lookup", {"token": self.token})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json["already_present_checkpoints"], [])
        self.assertEqual({key: lookup.json["attendee"][key] for key in (
            "day1_morning", "day1_evening", "day2_morning", "day2_evening"
        )}, {key: "Registered" for key in (
            "day1_morning", "day1_evening", "day2_morning", "day2_evening"
        )})
        # Lookup alone never writes attendance; the coordinator's separate update request does.
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Registered")
        update = self.post_json("/api/attendance", {
            "token": self.token, "checkpoint": "day2_evening", "status": "Present",
            "expected_status": "Registered",
        })
        self.assertEqual(update.status_code, 200)
        self.assertEqual(self.sheets.attendees[self.token]["day2_evening"], "Present")
        self.assertEqual(self.sheets.attendees[self.token]["day2_morning"], "Registered")
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Registered")

    def test_batch_updates_only_selected_checkpoints_and_is_idempotent(self):
        self.login()
        payload = {
            "token": self.token,
            "checkpoints": [
                {"checkpoint": "day1_morning", "expected_status": "Registered"},
                {"checkpoint": "day2_evening", "expected_status": "Registered"},
            ],
        }
        response = self.post_json("/api/attendance", payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["updated_checkpoints"], ["day1_morning", "day2_evening"])
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Present")
        self.assertEqual(self.sheets.attendees[self.token]["day2_evening"], "Present")
        self.assertEqual(self.sheets.attendees[self.token]["day1_evening"], "Registered")
        self.assertEqual(self.sheets.attendees[self.token]["day2_morning"], "Registered")
        repeated = self.post_json("/api/attendance", payload)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json["already_present"], ["day1_morning", "day2_evening"])
        self.assertEqual(repeated.json["updated_checkpoints"], [])

    def test_batch_rejects_empty_duplicate_or_invalid_checkpoint_sets(self):
        self.login()
        cases = [
            [],
            [{"checkpoint": "day1_morning", "expected_status": "Registered"},
             {"checkpoint": "day1_morning", "expected_status": "Registered"}],
            [{"checkpoint": "day3_morning", "expected_status": "Registered"}],
            [{"checkpoint": "day1_morning", "expected_status": "Absent"}],
        ]
        for checkpoints in cases:
            response = self.post_json("/api/attendance", {"token": self.token, "checkpoints": checkpoints})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(set(self.sheets.attendees[self.token][key] for key in (
            "day1_morning", "day1_evening", "day2_morning", "day2_evening"
        )), {"Registered"})

    def test_batch_conflict_reports_partial_updates_and_stops_safely(self):
        self.login()
        response = self.post_json("/api/attendance", {
            "token": self.token,
            "checkpoints": [
                {"checkpoint": "day1_morning", "expected_status": "Registered"},
                {"checkpoint": "day2_evening", "expected_status": "Present"},
            ],
        })
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json["partial"])
        self.assertEqual(response.json["updated_checkpoints"], ["day1_morning"])
        self.assertEqual(response.json["failed_checkpoint"], "day2_evening")
        self.assertEqual(response.json["code"], "conflict")
        self.assertEqual(response.json["current_status"], "Registered")
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Present")
        self.assertEqual(self.sheets.attendees[self.token]["day1_evening"], "Registered")

    def test_batch_sheet_failure_reports_partial_outcome_without_claiming_full_success(self):
        self.login()
        self.sheets.fail_checkpoint = "day1_evening"
        response = self.post_json("/api/attendance", {
            "token": self.token,
            "checkpoints": [
                {"checkpoint": "day1_morning", "expected_status": "Registered"},
                {"checkpoint": "day1_evening", "expected_status": "Registered"},
            ],
        })
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json["success"])
        self.assertTrue(response.json["partial"])
        self.assertEqual(response.json["updated_checkpoints"], ["day1_morning"])
        self.assertEqual(response.json["failed_checkpoint"], "day1_evening")
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Present")
        self.assertEqual(self.sheets.attendees[self.token]["day1_evening"], "Registered")

    def test_existing_attendance_can_be_corrected_without_old_status_confirmation(self):
        self.login()
        self.sheets.attendees[self.token]["day1_morning"] = "Present"
        lookup = self.post_json("/api/lookup", {"token": self.token})
        self.assertEqual(lookup.json["attendee"]["day1_morning"], "Present")
        self.assertEqual(lookup.json["already_present_checkpoints"], ["day1_morning"])
        self.assertEqual(lookup.json["attendee"]["day2_evening"], "Registered")
        duplicate = self.post_json("/api/attendance", {
            "token": self.token, "checkpoint": "day1_morning", "status": "Present",
            "expected_status": "Present",
        })
        self.assertEqual(duplicate.json["code"], "duplicate")
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Present")
        corrected = self.post_json("/api/attendance", {
            "token": self.token, "checkpoint": "day1_morning", "status": "Registered",
            "expected_status": "Present",
        })
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Registered")
        self.assertEqual(self.sheets.attendees[self.token]["day1_evening"], "Registered")

    def test_rejects_bad_day_status_and_stale_expected_value(self):
        self.login()
        self.assertEqual(self.post_json("/api/attendance", ["bad", "shape"]).status_code, 400)
        cases = [
            {"checkpoint": "day3_morning", "status": "Present", "expected_status": "Registered"},
            {"checkpoint": "day1_morning", "status": "Morning", "expected_status": "Registered"},
        ]
        for values in cases:
            response = self.post_json("/api/attendance", {"token": self.token, **values})
            self.assertEqual(response.status_code, 400)
        stale = self.post_json("/api/attendance", {
            "token": self.token, "checkpoint": "day1_morning", "status": "Registered",
            "expected_status": "Present", "confirm_status_change": True,
        })
        self.assertEqual(stale.status_code, 409)

    def test_simultaneous_expected_status_updates_do_not_overwrite(self):
        self.login()
        outcomes = []
        barrier = threading.Barrier(2)

        def update(status):
            barrier.wait()
            outcomes.append(self.sheets.update(self.token, "day1_morning", status, "Registered"))

        threads = [threading.Thread(target=update, args=(status,)) for status in ("Present", "Registered")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(bool(outcome.get("updated")) for outcome in outcomes), 1)
        self.assertEqual(sum(bool(outcome.get("conflict") or outcome.get("duplicate")) for outcome in outcomes), 1)

    def test_google_sheets_failure_is_reported(self):
        self.login()
        self.sheets.fail = True
        response = self.post_json("/api/lookup", {"token": self.token})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["code"], "sheets_error")

    def test_attendee_qr_list_requires_auth_and_uses_post(self):
        self.assertEqual(self.client.post("/api/attendees/qr").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/attendees/qr").status_code, 405)
        response = self.client.post("/api/attendees/qr", headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["attendees"], [])

    def test_apps_script_client_rejects_missing_configuration(self):
        with self.assertRaises(SheetsError):
            AppsScriptSheets("", "").lookup(self.token)

    @patch("app.requests.post")
    def test_apps_script_client_maps_unknown_qr_and_conflict(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        post.return_value = response

        response.json.return_value = {"error": True, "code": "not_found"}
        self.assertEqual(AppsScriptSheets("https://script.test/exec", "secret").lookup(self.token),
                         {"not_found": True})

        response.json.return_value = {
            "error": True, "code": "conflict", "current_status": "Present"
        }
        result = AppsScriptSheets("https://script.test/exec", "secret").update(
            self.token, "day2_evening", "Present", "Registered"
        )
        self.assertEqual(result, {"conflict": True, "current_status": "Present"})
        self.assertEqual(post.call_args.kwargs["json"], {
            "secret": "secret", "action": "update", "token": self.token,
            "checkpoint": "day2_evening", "status": "Present",
            "expected_status": "Registered",
        })

    def test_dashboard_shows_after_login_and_scanner_is_a_hidden_modal(self):
        self.login()
        self.assertEqual(self.client.get("/").status_code, 303)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Startup Conclave', response.data)
        self.assertIn(b'id="open-scanner"', response.data)
        self.assertIn(b'id="scanner-modal"', response.data)
        self.assertIn(b'id="scanner-modal" class="scanner-modal" role="dialog" aria-modal="true" aria-labelledby="scanner-title" hidden', response.data)
        self.assertIn(b'Attendee QR Codes', response.data)
        self.assertIn(b'QR Email Management', response.data)
        self.assertIn(b'id="reader"', response.data)
        self.assertIn(b'id="start-scan"', response.data)
        self.assertIn(b'id="stop-scan"', response.data)
        self.assertIn(b'id="toast" class="toast" role="status" aria-live="polite" aria-atomic="true" hidden', response.data)
        self.assertIn(b'id="switch-camera" type="button" hidden', response.data)
        self.assertIn(b'<header', response.data)
        self.assertIn(b'<footer', response.data)
        self.assertEqual(response.data.count(b'type="checkbox" name="scanner-checkpoint"'), 4)

    def test_dashboard_authentication_logout_and_no_signup(self):
        self.assertIn(b'Coordinator password', self.client.get("/").data)
        self.assertEqual(self.client.get("/dashboard").status_code, 303)
        self.assertEqual(self.client.get("/qrcodes").status_code, 303)
        self.assertEqual(self.client.get("/qr-email-test").status_code, 303)
        self.assertEqual(self.client.get("/signup").status_code, 404)
        login_response = self.client.post("/login", data={"password": "coordinator-pass"})
        self.assertEqual(login_response.headers["Location"], "/dashboard")
        with self.client.session_transaction() as state:
            token = state["csrf_token"]
        logout = self.client.post("/logout", data={"_csrf": token})
        self.assertEqual(logout.status_code, 303)
        self.assertEqual(logout.headers["Location"], "/")
        self.assertEqual(self.client.get("/dashboard").status_code, 303)

    def test_checkin_card_hides_status_history_and_requires_one_selection(self):
        self.login()
        response = self.client.get("/dashboard")
        self.assertNotIn(b"Existing status at all checkpoints", response.data)
        self.assertNotIn(b"Selected checkpoint status", response.data)
        self.assertIn(b'id="attendee-name"', response.data)
        self.assertIn(b'id="attendee-id"', response.data)
        self.assertIn(b'id="attendance-modal"', response.data)
        self.assertIn(b'Submit Attendance', response.data)
        self.assertEqual(response.data.count(b'type="checkbox" name="attendance-checkpoint"'), 4)
        rendered = response.data.decode("utf-8")
        for checkpoint in ("Day 1 \u2014 Morning", "Day 1 \u2014 Evening",
                           "Day 2 \u2014 Morning", "Day 2 \u2014 Evening"):
            self.assertIn(checkpoint, rendered)
        self.assertNotIn(b'<select', response.data)

    @patch("app.requests.post")
    def test_apps_script_client_reports_invalid_response(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("not JSON")
        post.return_value = response
        with self.assertRaises(SheetsError):
            AppsScriptSheets("https://script.test/exec", "secret").lookup(self.token)

    def test_missing_server_secrets_show_setup_page(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "", "COORDINATOR_PASSWORD": ""}, self.sheets)
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 503)

    def test_example_placeholder_credentials_show_setup_page(self):
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "replace-with-a-long-random-secret-value-that-is-long-enough",
            "COORDINATOR_PASSWORD": "replace-with-a-unique-coordinator-password",
        }, self.sheets)
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 503)

    def test_setup_page_regression_for_short_nonempty_environment_values(self):
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "s" * 31,
            "COORDINATOR_PASSWORD": "p" * 16,
        }, self.sheets)
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 503)

    def test_config_health_reports_only_boolean_presence_and_readiness(self):
        app = create_app({
            "TESTING": True, "SECRET_KEY": "", "COORDINATOR_PASSWORD": "",
        }, self.sheets)
        response = app.test_client().get("/health/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            "flask_secret_key_present": False,
            "coordinator_password_present": False,
            "setup_ready": False,
        })
        self.assertTrue(all(isinstance(value, bool) for value in response.json.values()))

    def test_whitespace_only_credentials_do_not_count_as_configured(self):
        app = create_app({
            "TESTING": True, "SECRET_KEY": " " * 40, "COORDINATOR_PASSWORD": " " * 20,
        }, self.sheets)
        response = app.test_client().get("/health/config")
        self.assertEqual(response.json["flask_secret_key_present"], False)
        self.assertEqual(response.json["coordinator_password_present"], False)
        self.assertEqual(response.json["setup_ready"], False)


    def test_qr_email_candidates_are_admin_only_capped_and_hide_recipients_and_tokens(self):
        response = self.client.post("/api/qr-email/candidates")
        self.assertEqual(response.status_code, 401)
        self.login()
        response = self.post_json("/api/qr-email/candidates", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["registration_id"] for item in response.json["candidates"]],
                         ["TEST-1", "TEST-2", "TEST-3"])
        self.assertEqual(response.json["candidates"][0]["email"], "***@example.test")
        self.assertNotIn("attendee1", response.get_data(as_text=True))

    def test_ambiguous_email_outcome_is_exposed_as_manual_review_not_retryable(self):
        self.login()
        self.app.config["QR_EMAIL_TEST_RECIPIENTS"] = {"attendee1@example.test"}
        row = self.sheets._qr_email_rows()[0]
        row["email_status"] = "Sending"
        row["email_error"] = "outcome_unknown"
        response = self.post_json("/api/qr-email/candidates", {})
        self.assertEqual(response.status_code, 200)
        candidate = response.json["candidates"][0]
        self.assertEqual(candidate["email_status"], "Manual review")
        self.assertEqual(candidate["email_error"], "outcome_unknown")
        self.assertNotIn("row_number", response.json["candidates"][0])
        self.assertNotIn("qr_token", response.json["candidates"][0])
        batch = self.post_json("/api/qr-email/batch", {"action": "all", "confirm_send": True})
        self.assertEqual(batch.json["manual_review_count"], 1)
        self.assertEqual(batch.json["sent_count"], 0)
        self.assertEqual(getattr(self.sheets, "email_sends", []), [])

    def test_retry_exhausted_email_is_manual_review_and_not_retried(self):
        self.login()
        self.app.config["QR_EMAIL_TEST_RECIPIENTS"] = {"attendee1@example.test"}
        row = self.sheets._qr_email_rows()[0]
        row["email_status"] = "Failed"
        row["email_error"] = "retry_exhausted"
        response = self.post_json("/api/qr-email/candidates", {})
        self.assertEqual(response.json["candidates"][0]["email_status"], "Manual review")
        batch = self.post_json("/api/qr-email/batch", {
            "action": "retry_failed", "confirm_send": True,
        })
        self.assertEqual(batch.json["manual_review_count"], 1)
        self.assertEqual(batch.json["sent_count"], 0)
        self.assertEqual(getattr(self.sheets, "email_sends", []), [])

    def test_qr_email_test_mode_must_be_enabled(self):
        self.login()
        self.app.config["QR_EMAIL_TEST_MODE"] = False
        response = self.post_json("/api/qr-email/candidates", {})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["code"], "email_test_disabled")

    def test_qr_email_test_mode_requires_an_approved_recipient_allowlist(self):
        self.login()
        self.app.config["QR_EMAIL_TEST_RECIPIENTS"] = set()
        candidates = self.post_json("/api/qr-email/candidates", {})
        self.assertEqual(candidates.json["candidates"], [])
        batch = self.post_json("/api/qr-email/batch", {"action": "all", "confirm_send": True})
        self.assertEqual(batch.json["sent_count"], 0)
        self.assertEqual(getattr(self.sheets, "email_sends", []), [])

    def test_qr_email_send_requires_confirmation_and_registration_ids(self):
        self.login()
        response = self.post_json("/api/qr-email/send", {"registration_id": "TEST-1"})
        self.assertEqual(response.status_code, 400)
        response = self.post_json("/api/qr-email/send", {"row_number": 5, "confirm_send": True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(getattr(self.sheets, "email_sends", []), [])

    @patch("app.qr_png_base64", return_value="iVBOR-test-generated-image")
    def test_qr_email_delivery_uses_existing_token_and_duplicate_is_blocked(self, make_qr):
        self.login()
        first = self.post_json("/api/qr-email/send", {
            "registration_id": "TEST-1", "confirm_send": True,
        })
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["sent_count"], 1)
        self.assertEqual(len(self.sheets.email_sends), 1)
        sent = self.sheets.email_sends[0]
        self.assertEqual(sent["qr_token"], self.sheets.email_rows[0]["qr_token"])
        self.assertEqual(sent["qr_png_base64"], "iVBOR-test-generated-image")
        make_qr.assert_called_once_with(self.sheets.email_rows[0]["qr_token"])
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Registered")
        self.assertTrue(self.sheets.email_rows[0]["email_timestamp"])
        self.assertNotIn("email", first.json)
        self.assertNotIn("qr_token", first.json)

        second = self.post_json("/api/qr-email/send", {
            "registration_id": "TEST-1", "confirm_send": True,
        })
        self.assertEqual(second.json["skipped_count"], 1)
        self.assertEqual(len(self.sheets.email_sends), 1)

    @patch("app.qr_png_base64", return_value="iVBOR-test-generated-image")
    def test_qr_email_send_all_skips_sent_and_retries_failed_only_by_request(self, _make_qr):
        self.login()
        self.app.config["QR_EMAIL_TEST_MODE"] = False
        self.app.config["QR_EMAIL_BULK_ENABLED"] = True
        rows = self.sheets._qr_email_rows()
        rows[0]["email_status"] = "Sent"
        rows[2]["email_status"] = "Failed"
        response = self.post_json("/api/qr-email/batch", {"action": "all", "confirm_send": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["sent_count"], 2)
        self.assertEqual(response.json["remaining_count"], 1)
        self.assertEqual([item["registration_id"] for item in self.sheets.email_sends],
                         ["TEST-2", "TEST-3"])
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Registered")
        self.assertEqual(rows[0]["email_status"], "Sent")

        next_batch = self.post_json("/api/qr-email/batch", {
            "action": "selected", "registration_ids": ["TEST-4"], "confirm_send": True,
        })
        self.assertEqual(next_batch.json["sent_count"], 1)
        self.assertEqual(self.sheets.email_sends[-1]["registration_id"], "TEST-4")

        rows[1]["email_status"] = "Failed"
        response = self.post_json("/api/qr-email/batch", {
            "action": "retry_failed", "confirm_send": True,
        })
        self.assertEqual(response.json["sent_count"], 1)
        self.assertEqual(self.sheets.email_sends[-1]["registration_id"], "TEST-2")

    @patch("app.qr_png_base64", return_value="iVBOR-test-generated-image")
    def test_qr_email_selected_uses_only_server_resolved_registration_ids(self, _make_qr):
        self.login()
        self.app.config["QR_EMAIL_TEST_MODE"] = False
        self.app.config["QR_EMAIL_BULK_ENABLED"] = True
        response = self.post_json("/api/qr-email/batch", {
            "action": "selected", "registration_ids": ["TEST-2"], "confirm_send": True,
            "email": "attacker@example.test", "qr_token": "forged",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["registration_id"] for item in self.sheets.email_sends], ["TEST-2"])
        invalid = self.post_json("/api/qr-email/batch", {
            "action": "selected", "registration_ids": ["UNKNOWN"], "confirm_send": True,
        })
        self.assertEqual(invalid.status_code, 400)

    @patch("app.qr_png_base64", return_value="iVBOR-test-generated-image")
    def test_qr_email_partial_failure_and_test_mode_three_recipient_cap(self, _make_qr):
        self.login()
        self.sheets.fail_email_ids = {"TEST-2"}
        response = self.post_json("/api/qr-email/batch", {"action": "all", "confirm_send": True})
        self.assertEqual(response.json["selected_count"], 3)
        self.assertEqual(response.json["sent_count"], 2)
        self.assertEqual(response.json["failed_count"], 1)
        self.assertEqual(self.sheets.email_rows[3]["email_status"], "")
        self.assertEqual(self.sheets.email_rows[1]["email_error"], "delivery_failed")

    def test_apps_script_email_code_rechecks_registration_and_serializes_duplicate_sends(self):
        source = Path("apps_script/Code.gs").read_text(encoding="utf-8")
        self.assertIn("if (!lock.tryLock(0)) return json({ error: true, code: 'email_in_progress' })", source)
        self.assertIn("item.registrationId === registrationId", source)
        self.assertIn("candidate.token !== token", source)
        self.assertIn("oldStatus === 'Sending'", source)
        self.assertIn("oldStatus === 'Sent'", source)
        self.assertIn("attachments: [blob]", source)

    @patch("app.qr_png_base64", return_value="iVBOR-test-generated-image")
    def test_qr_email_invalid_addresses_are_reported_skipped_without_send(self, _make_qr):
        self.login()
        self.app.config["QR_EMAIL_TEST_MODE"] = False
        self.app.config["QR_EMAIL_BULK_ENABLED"] = True
        self.sheets.email_rows = self.sheets._qr_email_rows()
        self.sheets.email_rows[1]["email_valid"] = False
        candidates = self.post_json("/api/qr-email/candidates", {})
        self.assertEqual(candidates.status_code, 200)
        self.assertFalse(candidates.json["candidates"][1]["email_valid"])
        self.assertEqual(candidates.json["candidates"][1]["email"], "Invalid or missing")
        response = self.post_json("/api/qr-email/batch", {"action": "all", "confirm_send": True})
        self.assertEqual(response.json["skipped_count"], 1)
        self.assertNotIn("TEST-2", [sent["registration_id"] for sent in self.sheets.email_sends])

    def test_qr_email_candidate_and_batch_routes_require_authentication_and_csrf(self):
        self.assertEqual(self.client.post("/api/qr-email/batch", json={"action": "all"}).status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/qr-email/batch", json={"action": "all"}).status_code, 403)

    @patch("app.requests.post")
    def test_apps_script_qr_email_client_uses_mocked_http_actions(self, mocked_post):
        response = Mock()
        response.json.return_value = {"candidates": []}
        mocked_post.return_value = response
        client = AppsScriptSheets("https://unit-test.invalid/exec", "test-only-secret")
        self.assertEqual(client.qr_email_candidates(), {"candidates": []})
        self.assertEqual(mocked_post.call_args.kwargs["json"]["action"], "qr_email_candidates")

        response.json.return_value = {"sent": True, "email_status": "Sent"}
        result = client.send_qr_email("REG-1", self.token, "test-image-data", retry_failed=True)
        self.assertTrue(result["sent"])
        self.assertEqual(mocked_post.call_args.kwargs["json"]["action"], "send_qr_email")
        self.assertEqual(mocked_post.call_args.kwargs["json"]["registration_id"], "REG-1")
        self.assertTrue(mocked_post.call_args.kwargs["json"]["retry_failed"])

    def test_apps_script_sender_uses_only_configured_authorized_alias_and_reusable_template(self):
        source = Path("apps_script/Code.gs").read_text(encoding="utf-8")
        self.assertIn("GmailApp.getAliases()", source)
        self.assertIn("mailOptions.from = sender.senderEmail", source)
        self.assertIn("MailApp.sendEmail(Object.assign", source)
        self.assertIn("sender_not_authorized", source)
        self.assertIn("candidate.registrationId", source)
        self.assertIn("attachments: [blob]", source)


if __name__ == "__main__":
    unittest.main()
