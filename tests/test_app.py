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


class AttendanceAppTests(unittest.TestCase):
    token = "QRTS1:0123456789abcdef0123456789abcdef"

    def setUp(self):
        self.sheets = FakeSheets()
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-secret-key-that-is-at-least-32-characters-long",
            "COORDINATOR_PASSWORD": "coordinator-pass",
            "SESSION_COOKIE_SECURE": False,
        }, self.sheets)
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
            unconfirmed = self.post_json("/api/attendance", {
                "token": self.token, "checkpoint": checkpoint, "status": "Registered",
                "expected_status": "Present",
            })
            self.assertEqual(unconfirmed.status_code, 428)
            registered = self.post_json("/api/attendance", {
                "token": self.token, "checkpoint": checkpoint, "status": "Registered",
                "expected_status": "Present", "confirm_status_change": True,
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

    def test_existing_attendance_requires_change_confirmation_and_warns_on_duplicate(self):
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
        unconfirmed = self.post_json("/api/attendance", {
            "token": self.token, "checkpoint": "day1_morning", "status": "Registered",
            "expected_status": "Present",
        })
        self.assertEqual(unconfirmed.status_code, 428)
        self.assertEqual(unconfirmed.json["code"], "status_change_confirmation_required")
        self.assertEqual(self.sheets.attendees[self.token]["day1_morning"], "Present")
        confirmed = self.post_json("/api/attendance", {
            "token": self.token, "checkpoint": "day1_morning", "status": "Registered",
            "expected_status": "Present", "confirm_status_change": True,
        })
        self.assertEqual(confirmed.status_code, 200)
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
        self.assertIn(b"Day 1 \xe2\x80\x93 Morning", response.data)

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


if __name__ == "__main__":
    unittest.main()
