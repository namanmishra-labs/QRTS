import threading
import unittest

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

    def lookup(self, token):
        if self.fail:
            raise SheetsError("Google Sheets could not be reached. Try again.")
        attendee = self.attendees.get(token)
        return {"attendee": dict(attendee)} if attendee else {"not_found": True}

    def update(self, token, day, status, expected_status):
        if self.fail:
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
                 "email_status": "", "email_timestamp": "", "email_error": ""},
                {"row_number": 3, "registration_id": "TEST-2", "name": "Test Attendee 2",
                 "email": "attendee2@example.test", "qr_token": "QRTS1:00000000-0000-4000-8000-000000000002",
                 "email_status": "", "email_timestamp": "", "email_error": ""},
                {"row_number": 4, "registration_id": "TEST-3", "name": "Test Attendee 3",
                 "email": "attendee3@example.test", "qr_token": "QRTS1:00000000-0000-4000-8000-000000000003",
                 "email_status": "", "email_timestamp": "", "email_error": ""},
                {"row_number": 5, "registration_id": "TEST-4", "name": "Outside Test Cohort",
                 "email": "outside@example.test", "qr_token": "QRTS1:00000000-0000-4000-8000-000000000004",
                 "email_status": "", "email_timestamp": "", "email_error": ""},
            ]
        return self.email_rows

    def qr_email_candidates(self):
        return {"candidates": [dict(row) for row in self._qr_email_rows()]}

    def send_qr_email(self, row_number, qr_token, qr_png_base64):
        row = next((item for item in self._qr_email_rows()
                    if item["row_number"] == row_number), None)
        if row is None:
            return {"error": True, "code": "email_not_eligible"}
        if row["email_status"]:
            return {"error": True, "code": "email_already_attempted"}
        self.email_sends = getattr(self, "email_sends", [])
        self.email_sends.append({"row_number": row_number, "qr_token": qr_token,
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

    def test_existing_attendance_can_be_corrected_without_old_status_confirmation(self):
        self.login()
        self.sheets.attendees[self.token]["day1_morning"] = "Present"
        lookup = self.post_json("/api/lookup", {"token": self.token})
        self.assertEqual(lookup.json["attendee"]["day1_morning"], "Present")
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

    def test_checkin_page_lists_all_four_independent_checkpoints(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for checkpoint in ("day1_morning", "day1_evening", "day2_morning", "day2_evening"):
            self.assertIn(checkpoint.encode(), response.data)
        self.assertIn(b"value=\"day1_morning\"><span>Day 1<br><strong>Morning", response.data)

    def test_checkin_card_hides_status_history_and_requires_one_selection(self):
        self.login()
        response = self.client.get("/")
        self.assertNotIn(b"Existing status at all checkpoints", response.data)
        self.assertNotIn(b"Selected checkpoint status", response.data)
        self.assertIn(b'id="attendee-name"', response.data)
        self.assertIn(b'id="attendee-id"', response.data)
        self.assertIn(b'id="attendance-modal"', response.data)
        self.assertIn(b'id="mark-present" type="checkbox"', response.data)
        self.assertIn(b'Mark attendance as Present', response.data)
        self.assertIn(b'Submit Attendance', response.data)
        self.assertEqual(response.data.count(b'type="radio" name="checkpoint"'), 4)
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
        self.assertEqual([item["row_number"] for item in response.json["candidates"]], [2, 3, 4])
        self.assertNotIn("email", response.json["candidates"][0])
        self.assertNotIn("qr_token", response.json["candidates"][0])

    def test_qr_email_test_mode_must_be_enabled(self):
        self.login()
        self.app.config["QR_EMAIL_TEST_MODE"] = False
        response = self.post_json("/api/qr-email/candidates", {})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["code"], "email_test_disabled")

    def test_qr_email_send_requires_confirmation_and_rejects_rows_after_first_three(self):
        self.login()
        response = self.post_json("/api/qr-email/send", {"row_number": 2})
        self.assertEqual(response.status_code, 400)
        response = self.post_json("/api/qr-email/send", {
            "row_number": 5, "confirm_send": True,
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(getattr(self.sheets, "email_sends", []), [])

    @patch("app.qr_png_base64", return_value="iVBOR-test-generated-image")
    def test_qr_email_delivery_uses_existing_token_and_duplicate_is_blocked(self, make_qr):
        self.login()
        first = self.post_json("/api/qr-email/send", {
            "row_number": 2, "confirm_send": True,
        })
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["email_status"], "Sent")
        self.assertEqual(len(self.sheets.email_sends), 1)
        sent = self.sheets.email_sends[0]
        self.assertEqual(sent["qr_token"], self.sheets.email_rows[0]["qr_token"])
        self.assertEqual(sent["qr_png_base64"], "iVBOR-test-generated-image")
        make_qr.assert_called_once_with(self.sheets.email_rows[0]["qr_token"])
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Registered")
        self.assertNotIn("email", first.json)
        self.assertNotIn("qr_token", first.json)

        second = self.post_json("/api/qr-email/send", {
            "row_number": 2, "confirm_send": True,
        })
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(self.sheets.email_sends), 1)

    @patch("app.requests.post")
    def test_apps_script_qr_email_client_uses_mocked_http_actions(self, mocked_post):
        response = Mock()
        response.json.return_value = {"candidates": []}
        mocked_post.return_value = response
        client = AppsScriptSheets("https://unit-test.invalid/exec", "test-only-secret")
        self.assertEqual(client.qr_email_candidates(), {"candidates": []})
        self.assertEqual(mocked_post.call_args.kwargs["json"]["action"], "qr_email_candidates")

        response.json.return_value = {"sent": True, "email_status": "Sent"}
        result = client.send_qr_email(2, self.token, "test-image-data")
        self.assertTrue(result["sent"])
        self.assertEqual(mocked_post.call_args.kwargs["json"]["action"], "send_qr_email")


if __name__ == "__main__":
    unittest.main()
