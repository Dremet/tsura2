"""Login return path and admin access behavior without external services."""

import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tsura.app import create_app
from tsura.app.blueprints.admin import calendar as calendar_routes
from tsura.app.blueprints.auth import routes as auth_routes
from tsura.app.extensions import db_pool


class FakeConnection:
    def __init__(self, row=None):
        self.row = row
        self.committed = False

    def cursor(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        pass

    def fetchone(self):
        return self.row

    def commit(self):
        self.committed = True


class AdminLoginRedirectTests(unittest.TestCase):
    def setUp(self):
        env = {
            "TSU_HOTLAPPING_POSTGRES_URL": "postgresql:///unused",
            "TSURA_SECRET_KEY": "test-only-secret",
            "TSURA_BASE_URL": "https://tsura.org",
        }
        with patch.dict(os.environ, env), patch.object(db_pool, "init_app"):
            self.app = create_app()
        self.client = self.app.test_client()

    def test_calendar_returns_to_original_path_after_steam_login(self):
        blocked = self.client.get("/admin/calendar?view=week")
        self.assertEqual(blocked.status_code, 302)
        self.assertEqual(urlsplit(blocked.location).path, "/auth/login")
        self.assertEqual(parse_qs(urlsplit(blocked.location).query)["next"],
                         ["/admin/calendar?view=week"])

        steam = self.client.get(blocked.location)
        self.assertEqual(steam.status_code, 302)
        self.assertEqual(urlsplit(steam.location).netloc, "steamcommunity.com")

        connection = FakeConnection()
        with patch.object(auth_routes, "_verify_openid", return_value=12345), \
                patch.object(db_pool, "get_conn", return_value=connection):
            callback = self.client.get("/auth/callback")
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(callback.location, "/admin/calendar?view=week")
        self.assertTrue(connection.committed)

    def test_other_admin_pages_also_request_login(self):
        for path in ("/admin/", "/career/admin"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(parse_qs(urlsplit(response.location).query)["next"],
                                 [path])

    def test_external_return_path_is_rejected(self):
        self.assertIsNone(auth_routes._safe_next("//evil.example"))
        self.assertIsNone(auth_routes._safe_next("/\\evil.example"))
        self.assertIsNone(auth_routes._safe_next("https://evil.example"))
        self.assertEqual(auth_routes._safe_next("/admin/calendar"),
                         "/admin/calendar")

    def test_logged_in_non_admin_still_gets_forbidden(self):
        self.client.set_cookie("tsura_sid", "test-session")
        connection = FakeConnection({"steam_id": 12345})
        with patch.object(db_pool, "get_conn", return_value=connection), \
                patch.object(calendar_routes, "user_admin_servers", return_value=[]):
            response = self.client.get("/admin/calendar")
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
