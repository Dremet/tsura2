"""The public month view can browse both future and historical race dates."""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tsura.app import create_app
from tsura.app.extensions import db_pool


class CalendarConnection:
    def __init__(self, events):
        self.events = events
        self.queries = []

    def cursor(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchall(self):
        return self.events


class PublicCalendarMonthTests(unittest.TestCase):
    def setUp(self):
        env = {
            "TSU_HOTLAPPING_POSTGRES_URL": "postgresql:///unused",
            "TSURA_SECRET_KEY": "test-only-secret",
        }
        with patch.dict(os.environ, env), patch.object(db_pool, "init_app"):
            self.app = create_app()

    def test_month_view_queries_visible_weeks_and_renders_past_event(self):
        event = {
            "id": 1, "starts_at": datetime(2026, 10, 3, 20, tzinfo=timezone.utc),
            "league_name": "One-off", "league_description": "Standalone race event.",
            "league_color": "oneoff", "details": "Autumn race",
        }
        conn = CalendarConnection([event])
        with patch.object(db_pool, "get_conn", return_value=conn):
            response = self.app.test_client().get("/calendar?view=month&month=2026-10")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("October 2026", html)
        self.assertIn("Autumn race", html)
        self.assertIn('data-month-day="2026-10-03"', html)
        self.assertIn("view=month&amp;month=2026-09", html)
        self.assertIn("view=month&amp;month=2026-11", html)
        self.assertEqual(len(conn.queries), 1)
        self.assertEqual(conn.queries[0][1], (
            datetime(2026, 9, 27, tzinfo=timezone.utc),
            datetime(2026, 11, 3, tzinfo=timezone.utc),
        ))

    def test_list_view_stays_available(self):
        conn = CalendarConnection([])
        with patch.object(db_pool, "get_conn", return_value=conn):
            response = self.app.test_client().get("/calendar?view=list")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Upcoming races", html)
        self.assertIn("Recent races", html)
        self.assertIn("Month view", html)
        self.assertNotIn('data-month-calendar', html)
        self.assertEqual(len(conn.queries), 2)

    def test_month_view_is_default_and_marks_today(self):
        conn = CalendarConnection([])
        with patch.object(db_pool, "get_conn", return_value=conn):
            response = self.app.test_client().get("/calendar")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-month-calendar', html)
        self.assertIn('class="calendar-month-day is-today"', html)
        self.assertIn('aria-current="date"', html)
        self.assertIn('view=list', html)
        self.assertNotIn('id="upcoming-races"', html)
        self.assertEqual(len(conn.queries), 1)

    def test_edge_event_is_available_for_local_day_repositioning(self):
        event = {
            "id": 2, "starts_at": datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc),
            "league_name": "One-off", "league_description": "Standalone race event.",
            "league_color": "oneoff", "details": "Midnight race",
        }
        conn = CalendarConnection([event])
        with patch.object(db_pool, "get_conn", return_value=conn):
            response = self.app.test_client().get("/calendar?view=month&month=2026-10")

        html = response.get_data(as_text=True)
        self.assertIn('data-month-overflow', html)
        self.assertIn("Midnight race", html)
        self.assertIn('data-event-start="2026-09-27T23:30:00+00:00"', html)


if __name__ == "__main__":
    unittest.main()
