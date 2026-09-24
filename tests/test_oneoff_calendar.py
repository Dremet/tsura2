"""Standalone calendar events never require a league or create a series."""

import os
import unittest
from unittest.mock import patch

from flask import g

from tsura.app import create_app
from tsura.app.blueprints.admin import calendar as calendar_routes
from tsura.app.blueprints.admin.routes import OWNER_STEAM_ID
from tsura.app.extensions import db_pool


class FakeConnection:
    def __init__(self):
        self.commands = []
        self.committed = False
        self.rolled_back = False

    def cursor(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.commands.append((sql, params))

    def fetchall(self):
        return []

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class OneOffCalendarTests(unittest.TestCase):
    def setUp(self):
        env = {
            "TSU_HOTLAPPING_POSTGRES_URL": "postgresql:///unused",
            "TSURA_SECRET_KEY": "test-only-secret",
        }
        with patch.dict(os.environ, env), patch.object(db_pool, "init_app"):
            self.app = create_app()

    def request(self, method="GET", repeat_count="1"):
        data = {
            "action": "create_event",
            "league_id": "one-off",
            "details": "Special race at Silverstone",
            "local_start": "2026-10-03T20:00",
            "timezone": "Europe/Berlin",
            "repeat_count": repeat_count,
        }
        conn = FakeConnection()
        with self.app.test_request_context("/admin/calendar", method=method,
                                           data=data if method == "POST" else None):
            g.current_steam_id = OWNER_STEAM_ID
            g.session_id = "test-session"
            with patch.object(db_pool, "get_conn", return_value=conn), \
                    patch.object(calendar_routes, "_csrf_ok", return_value=True):
                response = calendar_routes.calendar()
        return response, conn

    def test_one_off_option_works_without_any_leagues(self):
        html, _ = self.request()
        self.assertIn('value="one-off"', html)
        self.assertIn('id="event-submit">Create event(s)', html)
        self.assertEqual(html.count('type="radio" name="color_key"'), 6)

    def test_one_off_insert_has_no_league(self):
        response, conn = self.request(method="POST")
        inserts = [params for sql, params in conn.commands
                   if "INSERT INTO webadmin.calendar_events" in sql]
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(inserts), 1)
        self.assertIsNone(inserts[0][0])
        self.assertTrue(conn.committed)

    def test_one_off_repetition_is_rejected(self):
        response, conn = self.request(method="POST", repeat_count="6")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(conn.committed)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(any("INSERT INTO webadmin.calendar_events" in sql
                             for sql, _ in conn.commands))


if __name__ == "__main__":
    unittest.main()
