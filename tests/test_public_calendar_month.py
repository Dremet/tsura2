"""The public month view can browse both future and historical race dates."""

import os
import re
import unittest
from datetime import datetime, timezone
from html import unescape
from unittest.mock import patch

from tsura.app import create_app
from tsura.app.blueprints.main import routes as main_routes
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
            response = self.app.test_client().get("/calendar?view=month&date=2026-10-01")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("October 2026", html)
        self.assertIn("Autumn race", html)
        self.assertIn('data-month-day="2026-10-03"', html)
        self.assertIn('data-month-select-day', html)
        self.assertIn('data-month-agenda', html)
        self.assertIn('data-bs-toggle="tooltip" data-bs-container="body"', html)
        self.assertIn("view=month&amp;date=2026-09-01", html)
        self.assertIn("view=month&amp;date=2026-11-01", html)
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

    def test_list_events_show_a_countdown_on_the_left(self):
        event = {
            "id": 3, "starts_at": datetime(2099, 1, 1, 20, tzinfo=timezone.utc),
            "league_name": "One-off", "league_description": "Standalone race event.",
            "league_color": "oneoff", "details": "Future race",
        }
        conn = CalendarConnection([event])
        with patch.object(db_pool, "get_conn", return_value=conn):
            response = self.app.test_client().get("/calendar?view=list")

        html = response.get_data(as_text=True)
        self.assertIn('data-event-countdown="2099-01-01T20:00:00+00:00"', html)
        self.assertIn('data-countdown-value', html)
        self.assertLess(html.index('data-event-countdown'), html.index('data-calendar-date'))

    def test_week_view_shows_three_weeks_and_moves_one_week_at_a_time(self):
        event = {
            "id": 4, "starts_at": datetime(2026, 10, 20, 20, tzinfo=timezone.utc),
            "league_name": "One-off", "league_description": "Standalone race event.",
            "league_color": "oneoff", "details": "Week three race",
        }
        conn = CalendarConnection([event])
        with patch.object(db_pool, "get_conn", return_value=conn):
            response = self.app.test_client().get("/calendar?view=week&week=2026-10-07")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-week-key="2026-10-05"', html)
        self.assertEqual(html.count('class="calendar-week-panel"'), 3)
        self.assertEqual(html.count('data-calendar-day="'), 21)
        self.assertIn("Week three race", html)
        self.assertIn("view=week&amp;date=2026-09-30", html)
        self.assertIn("view=week&amp;date=2026-10-14", html)
        self.assertEqual(len(conn.queries), 1)
        self.assertEqual(conn.queries[0][1], (
            datetime(2026, 10, 4, tzinfo=timezone.utc),
            datetime(2026, 10, 27, tzinfo=timezone.utc),
        ))

    @staticmethod
    def view_link(html, view):
        match = re.search(r'href="([^"]+)"[^>]*>' + view.capitalize() + r' view</a>', html)
        if not match:
            raise AssertionError(f"Missing {view} view link")
        return unescape(match.group(1))

    def test_switching_views_preserves_date_across_month_and_year_boundaries(self):
        # These weeks start in the preceding month/year. Following real links
        # repeatedly must retain the focus date rather than adopt that Monday.
        for focus in ("2026-09-01", "2027-01-01", "2027-02-01"):
            with self.subTest(focus=focus), patch.object(
                    db_pool, "get_conn", return_value=CalendarConnection([])):
                client = self.app.test_client()
                html = client.get(f"/calendar?date={focus}").get_data(as_text=True)
                for view in ("week", "list", "month", "list", "week", "month"):
                    response = client.get(self.view_link(html, view))
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertIn(f'data-calendar-focus-date="{focus}"', html)
                    if view == "month":
                        self.assertIn(f'data-month-key="{focus[:7]}"', html)

    def test_current_month_switches_to_current_week(self):
        with patch.object(main_routes, "datetime", wraps=datetime) as clock, \
                patch.object(db_pool, "get_conn", return_value=CalendarConnection([])):
            clock.now.return_value = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
            client = self.app.test_client()
            html = client.get("/calendar?view=list&month=2026-09").get_data(as_text=True)
            html = client.get(self.view_link(html, "week")).get_data(as_text=True)
            self.assertIn('data-week-key="2026-09-28"', html)
            html = client.get(self.view_link(html, "month")).get_data(as_text=True)
            self.assertIn('data-month-key="2026-09"', html)

    def test_legacy_month_link_does_not_drift_via_week_and_list(self):
        with patch.object(db_pool, "get_conn", return_value=CalendarConnection([])):
            client = self.app.test_client()
            html = client.get("/calendar?month=2027-01").get_data(as_text=True)
            for view in ("week", "list", "month"):
                html = client.get(self.view_link(html, view)).get_data(as_text=True)
            self.assertIn('data-month-key="2027-01"', html)

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
