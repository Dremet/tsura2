"""Shared race calendar administration for every server admin."""

from datetime import timezone

import psycopg
from flask import abort, flash, g, redirect, render_template, request, url_for

from . import admin_bp
from .routes import _csrf_ok, _cur, user_admin_servers
from ...calendar_time import weekly_starts
from ...extensions import db_pool

COLOR_OPTIONS = (
    ("coral", "Coral"),
    ("teal", "Teal"),
    ("gold", "Gold"),
    ("violet", "Violet"),
)


def _required_text(field, label, maximum):
    value = (request.form.get(field) or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum} characters.")
    return value


def _id(field):
    try:
        value = int(request.form.get(field, ""))
    except ValueError:
        raise ValueError("Choose a valid league or event.") from None
    if value < 1:
        raise ValueError("Choose a valid league or event.")
    return value


def _event_starts(count):
    offset = request.form.get("utc_offset", "")
    try:
        offset = int(offset) if offset else None
    except ValueError:
        raise ValueError("Invalid system time zone offset.") from None
    if offset is not None and not -840 <= offset <= 840:
        raise ValueError("Invalid system time zone offset.")
    return weekly_starts(request.form.get("local_start", ""),
                         request.form.get("timezone", ""), count, offset)


@admin_bp.route("/calendar", methods=["GET", "POST"])
def calendar():
    if not user_admin_servers(g.get("current_steam_id")):
        abort(403)
    if request.method == "POST":
        if not _csrf_ok():
            abort(400)
        action = request.form.get("action")
        conn = db_pool.get_conn()
        try:
            with conn.cursor() as cur:
                if action in ("create_league", "update_league"):
                    name = _required_text("name", "League name", 80)
                    description = (request.form.get("description") or "").strip()
                    color_key = request.form.get("color_key", "")
                    if len(description) > 240:
                        raise ValueError("League description is limited to 240 characters.")
                    if color_key not in dict(COLOR_OPTIONS):
                        raise ValueError("Choose one of the available league colors.")
                    if action == "create_league":
                        cur.execute(
                            "INSERT INTO webadmin.leagues "
                            "(name, description, color_key, created_by) "
                            "VALUES (%s, %s, %s, %s)",
                            (name, description, color_key, g.current_steam_id))
                    else:
                        cur.execute(
                            "UPDATE webadmin.leagues SET name = %s, description = %s, "
                            "color_key = %s, updated_at = now() WHERE id = %s",
                            (name, description, color_key, _id("league_id")))
                        if not cur.rowcount:
                            raise ValueError("League no longer exists.")
                    message = "League saved."
                elif action in ("create_event", "update_event"):
                    league_id = _id("league_id")
                    details = _required_text("details", "Race details", 160)
                    cur.execute("SELECT 1 FROM webadmin.leagues WHERE id = %s", (league_id,))
                    if not cur.fetchone():
                        raise ValueError("Choose an existing league.")
                    if action == "create_event":
                        try:
                            count = int(request.form.get("repeat_count", "1"))
                        except ValueError:
                            raise ValueError("Choose between 1 and 26 events.") from None
                        starts = _event_starts(count)
                        for start in starts:
                            cur.execute(
                                "INSERT INTO webadmin.calendar_events "
                                "(league_id, details, starts_at, created_by) "
                                "VALUES (%s, %s, %s, %s)",
                                (league_id, details, start, g.current_steam_id))
                        message = f"Created {len(starts)} race event(s)."
                    else:
                        start = _event_starts(1)[0]
                        cur.execute(
                            "UPDATE webadmin.calendar_events "
                            "SET league_id = %s, details = %s, starts_at = %s, "
                            "updated_at = now() WHERE id = %s",
                            (league_id, details, start, _id("event_id")))
                        if not cur.rowcount:
                            raise ValueError("Event no longer exists.")
                        message = "Event updated."
                elif action == "delete_event":
                    cur.execute("DELETE FROM webadmin.calendar_events WHERE id = %s",
                                (_id("event_id"),))
                    if not cur.rowcount:
                        raise ValueError("Event no longer exists.")
                    message = "Event deleted."
                else:
                    abort(400)
            conn.commit()
            flash(message, "success")
        except (ValueError, psycopg.errors.UniqueViolation,
                psycopg.errors.ForeignKeyViolation) as exc:
            conn.rollback()
            if isinstance(exc, psycopg.errors.UniqueViolation):
                flash("A league with this name already exists.", "danger")
            elif isinstance(exc, psycopg.errors.ForeignKeyViolation):
                flash("The selected league is no longer available.", "danger")
            else:
                flash(str(exc), "danger")
        return redirect(url_for("admin.calendar"))

    with _cur() as cur:
        cur.execute("SELECT id, name, description, color_key "
                    "FROM webadmin.leagues ORDER BY lower(name)")
        leagues = cur.fetchall()
        event_sql = (
            "SELECT e.id, e.league_id, e.details, e.starts_at, l.name AS league_name "
            "FROM webadmin.calendar_events e JOIN webadmin.leagues l ON l.id = e.league_id "
        )
        cur.execute(event_sql + "WHERE e.starts_at >= now() ORDER BY e.starts_at, e.id")
        upcoming = cur.fetchall()
        cur.execute(event_sql + "WHERE e.starts_at < now() ORDER BY e.starts_at DESC LIMIT 20")
        past = cur.fetchall()
    return render_template("admin/calendar.html", leagues=leagues,
                           color_options=COLOR_OPTIONS,
                           upcoming=upcoming, past=past, utc=timezone.utc)
