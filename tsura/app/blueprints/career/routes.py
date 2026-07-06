"""Career routes: public displays + logged-in car tuning + admin season mgmt.

Reads career.* config tables and mart.v_career_* views (SELECT granted to the
`tsura` role); writes seasons/enrollments/upgrades to career.* (write granted).
Race results + rewards are produced by the pipeline and read via mart views.
"""
from __future__ import annotations

import hmac
import io
import os
import sys
import tempfile
from functools import wraps

import psycopg
from flask import (abort, current_app, flash, g, redirect, render_template,
                   request, send_file, url_for)

from . import career_bp
from ...extensions import db_pool, make_csrf_token

AXES_PERFORMANCE = ["top_speed", "acceleration", "braking", "grip", "downforce"]
AXES_DRIVEABILITY = ["sliding_gradual_range", "spring_max_length",
                     "locking_start_time", "oversteering_braking"]
AXES = AXES_PERFORMANCE + AXES_DRIVEABILITY
AXIS_LABELS = {
    "top_speed": "Top Speed",
    "acceleration": "Acceleration",
    "braking": "Braking",
    "grip": "Grip",
    "downforce": "Downforce",
    "sliding_gradual_range": "Sliding Gradual Range",
    "spring_max_length": "Spring Max Length",
    "locking_start_time": "Locking Start Time",
    "oversteering_braking": "Oversteering Braking",
}
# What each parameter actually does in-game (game's own physics tooltips,
# see tsu_vehicle_tools TOOLTIPS.md / tooltips.json).
def _field_grade(tier, max_tier, field_max_tier):
    """Competitive grade for one axis, relative to the field.

    F = stock (no investment); S (purple) = maxed out; A = field-leading
    (no other car has a higher value, and not maxed); B..E = ranked by how
    the tier compares to the strongest car on this axis.
    """
    if tier <= 0:
        return ("F", "#6e7681")
    if max_tier and tier >= max_tier:
        return ("S", "#a371f7")           # maxed out
    if tier >= field_max_tier:
        return ("A", "#2ea043")           # nobody higher (non-maxed leader)
    r = tier / field_max_tier if field_max_tier else 0
    if r > 0.75: return ("B", "#57ab5a")
    if r > 0.50: return ("C", "#d9a406")
    if r > 0.25: return ("D", "#e0863a")
    return ("E", "#d0553f")


AXIS_DESCRIPTIONS = {
    "top_speed": "Maximum speed in km/h on default tarmac.",
    "acceleration": "How quickly the car accelerates towards its top speed.",
    "braking": "Braking deceleration in m/s\u00b2 \u2014 lets you brake later and harder.",
    "grip": "How quickly the car can turn without starting to slide "
            "(turn rate = grip / speed).",
    "downforce": "Downward force from speed: extra grip and braking at high "
                 "speed, scaling with (speed / top speed)\u00b2.",
    "sliding_gradual_range": "Sliding effects build up gradually over this "
                             "range instead of hitting at full force \u2014 "
                             "slides get easier to catch.",
    "spring_max_length": "Longer suspension travel \u2014 the car stays more "
                         "settled over bumps, curbs and jumps.",
    "locking_start_time": "How long you can brake at full force before the "
                          "wheels lock up and start smoking.",
    "oversteering_braking": "Extra oversteer while braking \u2014 upgrades "
                            "bring it towards zero for a calmer rear end on "
                            "corner entry.",
}

# Server-side vehicle tools (deployed from tsura_server_scripts/career/);
# used to build the downloadable .veh from the driver's current tuning.
CAREER_TOOLS_DIR = "/home/career/career_tools"


def _career_vehicles():
    if CAREER_TOOLS_DIR not in sys.path:
        sys.path.insert(0, CAREER_TOOLS_DIR)
    import career_vehicles
    return career_vehicles


# ----------------------------------------------------------------- helpers
def _cur():
    return db_pool.get_conn().cursor(row_factory=psycopg.rows.dict_row)


def _csrf_ok() -> bool:
    submitted = request.form.get("csrf_token", "")
    sid = g.get("session_id")
    if not sid:
        return False
    expected = make_csrf_token(sid, current_app.config["SECRET_KEY"])
    return hmac.compare_digest(submitted, expected)


def _is_admin(steam_id) -> bool:
    if not steam_id:
        return False
    with _cur() as cur:
        cur.execute("SELECT 1 FROM career.admins WHERE steam_id = %s", (steam_id,))
        return cur.fetchone() is not None


def _is_participant(steam_id) -> bool:
    """During the beta, only allow-listed Steam IDs may join / tune."""
    if not steam_id:
        return False
    with _cur() as cur:
        cur.execute("SELECT 1 FROM career.allowed_participants WHERE steam_id = %s",
                    (steam_id,))
        return cur.fetchone() is not None


def _admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not _is_admin(g.get("current_steam_id")):
            abort(403)
        return f(*a, **k)
    return wrapper


def _login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not g.get("current_steam_id"):
            return redirect(url_for("auth.login"))
        return f(*a, **k)
    return wrapper


def _active_season(cur):
    cur.execute("SELECT * FROM career.seasons WHERE status = 'active' LIMIT 1")
    return cur.fetchone()


def _seasons(cur):
    cur.execute("SELECT * FROM career.seasons ORDER BY created_at DESC")
    return cur.fetchall()


# ------------------------------------------------------------------- public
@career_bp.route("/")
def home():
    with _cur() as cur:
        season = _active_season(cur)
        standings, balance, enrolled = [], None, False
        if season:
            cur.execute(
                "SELECT * FROM mart.v_career_standings WHERE season_id = %s "
                "ORDER BY points_total DESC, wins DESC LIMIT 10", (season["id"],))
            from ..main.routes import _flag_code
            standings = [dict(r, flag_code=_flag_code(r.get("driver_flag")))
                         for r in cur.fetchall()]
            sid = g.get("current_steam_id")
            if sid:
                cur.execute("SELECT 1 FROM career.enrollments "
                            "WHERE season_id = %s AND steam_id = %s",
                            (season["id"], sid))
                enrolled = cur.fetchone() is not None
                if enrolled:
                    cur.execute("SELECT balance FROM mart.v_career_credit_balance "
                                "WHERE season_id = %s AND steam_id = %s",
                                (season["id"], sid))
                    row = cur.fetchone()
                    balance = row["balance"] if row else None
    return render_template("career/home.html", season=season, standings=standings,
                           balance=balance, enrolled=enrolled,
                           is_admin=_is_admin(g.get("current_steam_id")),
                           is_participant=_is_participant(g.get("current_steam_id")))


@career_bp.route("/standings")
def standings():
    with _cur() as cur:
        seasons = _seasons(cur)
        sel = request.args.get("season", type=int)
        season = next((s for s in seasons if s["id"] == sel), None) \
            or _active_season(cur) or (seasons[0] if seasons else None)
        rows, penalties = [], []
        if season:
            cur.execute(
                "SELECT * FROM mart.v_career_standings WHERE season_id = %s "
                "ORDER BY points_total DESC, wins DESC", (season["id"],))
            rows = cur.fetchall()
            cur.execute(
                "SELECT p.steam_id, p.points, p.reason, p.created_at, "
                "  (SELECT dc.driver_name FROM mart.v_career_driver_cars dc "
                "    WHERE dc.season_id = p.season_id AND dc.steam_id = p.steam_id "
                "    LIMIT 1) AS driver_name "
                "FROM career.penalties p WHERE p.season_id = %s "
                "ORDER BY p.created_at DESC", (season["id"],))
            penalties = cur.fetchall()
    return render_template("career/standings.html", seasons=seasons, penalties=penalties,
                           season=season, rows=rows)


@career_bp.route("/upgrades")
def upgrades():
    with _cur() as cur:
        seasons = _seasons(cur)
        sel = request.args.get("season", type=int)
        season = next((s for s in seasons if s["id"] == sel), None) \
            or _active_season(cur) or (seasons[0] if seasons else None)
        table, axes_cfg = [], []
        if season:
            cur.execute("SELECT * FROM career.upgrade_axes WHERE season_id = %s",
                        (season["id"],))
            axes_cfg = {r["axis"]: r for r in cur.fetchall()}
            cur.execute(
                "SELECT steam_id, driver_name, axis, tier, final_value "
                "FROM mart.v_career_upgrades WHERE season_id = %s "
                "ORDER BY driver_name", (season["id"],))
            urows = cur.fetchall()
            field_max = {}   # axis -> highest tier anyone runs (field strength)
            for r in urows:
                field_max[r["axis"]] = max(field_max.get(r["axis"], 0), r["tier"])
            per_driver = {}
            for r in urows:
                d = per_driver.setdefault(
                    r["steam_id"],
                    {"driver_name": r["driver_name"], "steam_id": r["steam_id"],
                     "axes": {}, "spent": 0})
                _cfg = axes_cfg.get(r["axis"]) or {}
                gr, col = _field_grade(r["tier"], _cfg.get("max_tier"),
                                       field_max.get(r["axis"], 0))
                r["grade"], r["gcolor"] = gr, col
                d["axes"][r["axis"]] = r
                d["spent"] = d.get("spent", 0) + r["tier"] * (_cfg.get("cost_per_tier") or 0)
            table = sorted(per_driver.values(),
                           key=lambda d: d.get("spent", 0), reverse=True)
    return render_template("career/upgrades.html", seasons=seasons, season=season,
                           table=table, axes=AXES, axis_labels=AXIS_LABELS,
                           axes_cfg=axes_cfg)


@career_bp.route("/results")
def results():
    with _cur() as cur:
        cur.execute("""
            SELECT r.session_id, r.utc_start_time, r.track_name, r.season_id,
                   MIN(r.participant_count) AS participants,
                   MIN(r.driver_name) FILTER (WHERE r.position = 1) AS winner,
                   MIN(r.steam_id) FILTER (WHERE r.position = 1) AS winner_steam_id
              FROM mart.v_career_results r
          GROUP BY r.session_id, r.utc_start_time, r.track_name, r.season_id
          ORDER BY r.utc_start_time DESC LIMIT 100""")
        sessions = cur.fetchall()
    return render_template("career/results.html", sessions=sessions)


@career_bp.route("/results/<session_id>")
def result_detail(session_id):
    with _cur() as cur:
        cur.execute("SELECT * FROM mart.v_career_results WHERE session_id = %s "
                    "ORDER BY position", (session_id,))
        rows = cur.fetchall()
    if not rows:
        abort(404)
    return render_template("career/result_detail.html", rows=rows,
                           header=rows[0])


# ------------------------------------------------------------- garage / tuning
@career_bp.route("/garage")
@_login_required
def garage():
    sid = g.current_steam_id
    if not _is_participant(sid):
        return render_template("career/garage.html", season=None, not_allowed=True)
    with _cur() as cur:
        season = _active_season(cur)
        if not season:
            return render_template("career/garage.html", season=None)
        cur.execute("SELECT 1 FROM career.enrollments "
                    "WHERE season_id = %s AND steam_id = %s", (season["id"], sid))
        enrolled = cur.fetchone() is not None
        if not enrolled:
            return render_template("career/garage.html", season=season,
                                   enrolled=False)
        cur.execute("SELECT balance FROM mart.v_career_credit_balance "
                    "WHERE season_id = %s AND steam_id = %s", (season["id"], sid))
        row = cur.fetchone()
        balance = row["balance"] if row else 0
        cur.execute("SELECT * FROM career.upgrade_axes WHERE season_id = %s",
                    (season["id"],))
        cfg = {r["axis"]: r for r in cur.fetchall()}
        cur.execute("SELECT axis, tier FROM career.driver_upgrades "
                    "WHERE season_id = %s AND steam_id = %s", (season["id"], sid))
        tiers = {r["axis"]: r["tier"] for r in cur.fetchall()}
        cur.execute("SELECT axis, tier_after FROM career.last_purchase "
                    "WHERE season_id = %s AND steam_id = %s AND NOT undone",
                    (season["id"], sid))
        lp = cur.fetchone()
        undo_info = None
        if lp and tiers.get(lp["axis"], 0) == lp["tier_after"] and lp["axis"] in cfg:
            undo_info = {"label": AXIS_LABELS.get(lp["axis"], lp["axis"]),
                         "cost": cfg[lp["axis"]]["cost_per_tier"]}
        items = []
        for axis in AXES:
            c = cfg.get(axis)
            if not c:
                continue
            tier = tiers.get(axis, 0)
            items.append({
                "axis": axis, "label": AXIS_LABELS[axis], "tier": tier,
                "descr": AXIS_DESCRIPTIONS.get(axis, ""),
                "max_tier": c["max_tier"], "cost": c["cost_per_tier"],
                "base_value": c["base_value"], "step": c["step_per_tier"],
                "current_value": c["base_value"] + tier * c["step_per_tier"],
                "next_value": c["base_value"] + (tier + 1) * c["step_per_tier"],
                "can_buy": tier < c["max_tier"] and balance >= c["cost_per_tier"],
                "maxed": tier >= c["max_tier"],
            })
    sections = [
        ("Performance", None,
         [i for i in items if i["axis"] in AXES_PERFORMANCE]),
        ("Driveability",
         "Cheaper upgrades that don't gain much pace but make the car "
         "easier to drive.",
         [i for i in items if i["axis"] in AXES_DRIVEABILITY]),
    ]
    return render_template("career/garage.html", season=season, enrolled=True,
                           balance=balance, items=items, sections=sections,
                           undo_info=undo_info)


@career_bp.route("/garage/download")
@_login_required
def download_car():
    """The logged-in driver's .veh, built live from their current tuning."""
    sid = g.current_steam_id
    with _cur() as cur:
        season = _active_season(cur)
        if not season:
            abort(404)
        cur.execute("SELECT driver_name, axis, final_value "
                    "FROM mart.v_career_driver_cars "
                    "WHERE season_id = %s AND steam_id = %s",
                    (season["id"], sid))
        rows = cur.fetchall()
    if not rows:
        abort(404)
    veh_name = f"Career {rows[0]['driver_name']}"
    tuned = {r["axis"]: float(r["final_value"]) for r in rows}
    try:
        cv = _career_vehicles()
    except ImportError:
        current_app.logger.exception("career vehicle tools unavailable")
        abort(503)
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "car.veh")
        cv.build_driver_vehicle(season["base_vehicle_veh"], out,
                                display_name=veh_name, steam_id64=int(sid),
                                tuned=tuned)
        with open(out, "rb") as f:
            data = f.read()
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=f"{veh_name.replace(' ', '_')}.veh",
                     mimetype="application/octet-stream")


@career_bp.route("/join", methods=["POST"])
@_login_required
def join():
    if not _csrf_ok():
        abort(403)
    sid = g.current_steam_id
    if not _is_participant(sid):
        flash("TSU Career is invite-only during the beta. Ask an admin to add you.",
              "warning")
        return redirect(url_for("career.home"))
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM career.seasons WHERE status = 'active' LIMIT 1")
        s = cur.fetchone()
        if not s:
            flash("No active season to join.", "warning")
            return redirect(url_for("career.home"))
        cur.execute("INSERT INTO career.enrollments (season_id, steam_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING", (s[0], sid))
    conn.commit()
    flash("Welcome to TSU Career! You can now tune your car.", "success")
    return redirect(url_for("career.garage"))


@career_bp.route("/garage/buy", methods=["POST"])
@_login_required
def buy():
    if not _csrf_ok():
        abort(403)
    axis = request.form.get("axis", "")
    if axis not in AXES:
        abort(400)
    sid = g.current_steam_id
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        season = _active_season(cur)
        if not season:
            flash("No active season.", "warning")
            return redirect(url_for("career.garage"))
        cur.execute("SELECT 1 FROM career.enrollments "
                    "WHERE season_id = %s AND steam_id = %s", (season["id"], sid))
        if not cur.fetchone():
            abort(403)
        cur.execute("SELECT * FROM career.upgrade_axes "
                    "WHERE season_id = %s AND axis = %s", (season["id"], axis))
        cfg = cur.fetchone()
        if not cfg:
            abort(400)
        cur.execute("SELECT tier FROM career.driver_upgrades "
                    "WHERE season_id = %s AND steam_id = %s AND axis = %s",
                    (season["id"], sid, axis))
        row = cur.fetchone()
        tier = row["tier"] if row else 0
        cur.execute("SELECT balance FROM mart.v_career_credit_balance "
                    "WHERE season_id = %s AND steam_id = %s", (season["id"], sid))
        brow = cur.fetchone()
        balance = brow["balance"] if brow else 0

        if tier >= cfg["max_tier"]:
            flash(f"{AXIS_LABELS[axis]} is already maxed out.", "warning")
        elif balance < cfg["cost_per_tier"]:
            flash("Not enough credits for that upgrade.", "warning")
        else:
            cur.execute(
                "INSERT INTO career.driver_upgrades (season_id, steam_id, axis, tier) "
                "VALUES (%s, %s, %s, 1) "
                "ON CONFLICT (season_id, steam_id, axis) "
                "DO UPDATE SET tier = career.driver_upgrades.tier + 1, updated_at = now()",
                (season["id"], sid, axis))
            cur.execute(
                "INSERT INTO career.last_purchase "
                "(season_id, steam_id, axis, tier_after, bought_at, undone) "
                "VALUES (%s, %s, %s, %s, now(), false) "
                "ON CONFLICT (season_id, steam_id) "
                "DO UPDATE SET axis = EXCLUDED.axis, "
                "tier_after = EXCLUDED.tier_after, bought_at = now(), "
                "undone = false",
                (season["id"], sid, axis, tier + 1))
            flash(f"{AXIS_LABELS[axis]} upgraded!", "success")
    conn.commit()
    return redirect(url_for("career.garage"))


@career_bp.route("/garage/undo", methods=["POST"])
@_login_required
def undo():
    """Revert the driver's most recent purchase (one step only, no chaining)."""
    if not _csrf_ok():
        abort(403)
    sid = g.current_steam_id
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        season = _active_season(cur)
        if not season:
            flash("No active season.", "warning")
            return redirect(url_for("career.garage"))
        cur.execute("SELECT * FROM career.last_purchase "
                    "WHERE season_id = %s AND steam_id = %s AND NOT undone "
                    "FOR UPDATE", (season["id"], sid))
        lp = cur.fetchone()
        if not lp:
            conn.commit()
            flash("Nothing to undo \u2014 only your most recent upgrade "
                  "can be undone.", "warning")
            return redirect(url_for("career.garage"))
        cur.execute("SELECT tier FROM career.driver_upgrades "
                    "WHERE season_id = %s AND steam_id = %s AND axis = %s "
                    "FOR UPDATE", (season["id"], sid, lp["axis"]))
        row = cur.fetchone()
        if not row or row["tier"] != lp["tier_after"]:
            # stale (e.g. purchases were reset by an admin) -> invalidate
            cur.execute("UPDATE career.last_purchase SET undone = true "
                        "WHERE season_id = %s AND steam_id = %s",
                        (season["id"], sid))
            flash("Nothing to undo.", "warning")
        else:
            if lp["tier_after"] <= 1:
                cur.execute("DELETE FROM career.driver_upgrades "
                            "WHERE season_id = %s AND steam_id = %s "
                            "AND axis = %s", (season["id"], sid, lp["axis"]))
            else:
                cur.execute("UPDATE career.driver_upgrades "
                            "SET tier = tier - 1, updated_at = now() "
                            "WHERE season_id = %s AND steam_id = %s "
                            "AND axis = %s", (season["id"], sid, lp["axis"]))
            cur.execute("UPDATE career.last_purchase SET undone = true "
                        "WHERE season_id = %s AND steam_id = %s",
                        (season["id"], sid))
            cur.execute("SELECT cost_per_tier FROM career.upgrade_axes "
                        "WHERE season_id = %s AND axis = %s",
                        (season["id"], lp["axis"]))
            c = cur.fetchone()
            refund = c["cost_per_tier"] if c else 0
            flash(f"{AXIS_LABELS.get(lp['axis'], lp['axis'])} upgrade undone "
                  f"\u2014 {refund} cr refunded.", "success")
    conn.commit()
    return redirect(url_for("career.garage"))


# ------------------------------------------------------------------- admin
@career_bp.route("/admin")
@_admin_required
def admin():
    with _cur() as cur:
        seasons = _seasons(cur)
        for s in seasons:
            cur.execute("SELECT axis, base_value, step_per_tier, max_tier, cost_per_tier "
                        "FROM career.upgrade_axes WHERE season_id = %s", (s["id"],))
            s["axes"] = {r["axis"]: r for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS n FROM career.enrollments WHERE season_id = %s",
                        (s["id"],))
            s["enrolled"] = cur.fetchone()["n"]
        cur.execute("SELECT * FROM mart.v_career_participants ORDER BY added_at DESC")
        participants = cur.fetchall()
        cur.execute(
            "SELECT p.id, p.season_id, p.steam_id, p.points, p.reason, "
            "       p.created_at, s.name AS season_name, "
            "       (SELECT dc.driver_name FROM mart.v_career_driver_cars dc "
            "         WHERE dc.season_id = p.season_id AND dc.steam_id = p.steam_id "
            "         LIMIT 1) AS driver_name "
            "FROM career.penalties p "
            "JOIN career.seasons s ON s.id = p.season_id "
            "ORDER BY p.created_at DESC")
        penalties = cur.fetchall()
        pen_season = _active_season(cur)
        pen_drivers = []
        if pen_season:
            cur.execute(
                "SELECT DISTINCT steam_id, driver_name AS name "
                "FROM mart.v_career_driver_cars "
                "WHERE season_id = %s ORDER BY driver_name", (pen_season["id"],))
            pen_drivers = cur.fetchall()
        sdef = None
        if pen_season:
            cur.execute("SELECT start_credits, credit_first, credit_last "
                        "FROM career.seasons WHERE id = %s", (pen_season["id"],))
            _sc = cur.fetchone()
            cur.execute("SELECT axis, base_value, step_per_tier, max_tier, cost_per_tier "
                        "FROM career.upgrade_axes WHERE season_id = %s", (pen_season["id"],))
            sdef = {"start_credits": _sc["start_credits"],
                    "credit_first": _sc["credit_first"], "credit_last": _sc["credit_last"],
                    "axes": {r["axis"]: r for r in cur.fetchall()}}
        cur.execute(
            "SELECT r.id, r.steam_id, r.status, r.requested_at, r.processed_at, "
            "  r.note, (SELECT dc.driver_name FROM mart.v_career_driver_cars dc "
            "    WHERE dc.season_id=r.season_id AND dc.steam_id=r.steam_id LIMIT 1) "
            "    AS driver_name "
            "FROM career.car_build_requests r ORDER BY r.requested_at DESC LIMIT 10")
        build_requests = cur.fetchall()
    return render_template("career/admin.html", seasons=seasons, axes=AXES,
                           build_requests=build_requests, sdef=sdef,
                           axis_labels=AXIS_LABELS, participants=participants,
                           penalties=penalties, pen_drivers=pen_drivers,
                           pen_season=pen_season)


@career_bp.route("/admin/penalty/add", methods=["POST"])
@_admin_required
def admin_add_penalty():
    if not _csrf_ok():
        abort(403)
    f = request.form
    try:
        season_id = int(f["season_id"])
        sid = int(f["steam_id"])
        points = int(f["points"])
        if points <= 0:
            raise ValueError
    except (KeyError, ValueError):
        flash("Invalid penalty — points must be a positive integer.", "danger")
        return redirect(url_for("career.admin"))
    reason = (f.get("reason") or "").strip() or None
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO career.penalties (season_id, steam_id, points, reason, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (season_id, sid, points, reason, g.current_steam_id))
    conn.commit()
    flash(f"Penalty of {points} points applied (standings only).", "success")
    return redirect(url_for("career.admin"))


@career_bp.route("/admin/penalty/<int:penalty_id>/remove", methods=["POST"])
@_admin_required
def admin_remove_penalty(penalty_id):
    if not _csrf_ok():
        abort(403)
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM career.penalties WHERE id = %s", (penalty_id,))
    conn.commit()
    flash("Penalty removed.", "success")
    return redirect(url_for("career.admin"))


@career_bp.route("/admin/build-car", methods=["POST"])
@_admin_required
def admin_build_car():
    if not _csrf_ok():
        abort(403)
    f = request.form
    try:
        season_id = int(f["season_id"])
        sid = int(f["steam_id"])
    except (KeyError, ValueError):
        flash("Invalid build request.", "danger")
        return redirect(url_for("career.admin"))
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO career.car_build_requests (season_id, steam_id, requested_by) "
            "VALUES (%s, %s, %s)", (season_id, sid, g.current_steam_id))
    conn.commit()
    flash("Car build queued — it will be generated and forced in-game "
          "within ~1 minute.", "success")
    return redirect(url_for("career.admin"))


@career_bp.route("/admin/upgrades/reset", methods=["POST"])
@_admin_required
def admin_reset_upgrades():
    """Wipe a driver's purchased upgrades for one season.

    Refund is implicit: the credit balance derives 'spent' from the current
    tiers, so deleting them returns the full amount. last_purchase is
    invalidated so the garage undo button cannot act on stale state.
    """
    if not _csrf_ok():
        abort(403)
    f = request.form
    try:
        season_id = int(f["season_id"])
        sid = int(f["steam_id"])
    except (KeyError, ValueError):
        flash("Invalid reset request.", "danger")
        return redirect(url_for("career.admin"))
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT COALESCE(SUM(du.tier * ax.cost_per_tier), 0) AS refund, "
            "       COALESCE(SUM(du.tier), 0) AS tiers "
            "FROM career.driver_upgrades du "
            "JOIN career.upgrade_axes ax "
            "  ON ax.season_id = du.season_id AND ax.axis = du.axis "
            "WHERE du.season_id = %s AND du.steam_id = %s", (season_id, sid))
        agg = cur.fetchone()
        cur.execute("DELETE FROM career.driver_upgrades "
                    "WHERE season_id = %s AND steam_id = %s", (season_id, sid))
        cur.execute("UPDATE career.last_purchase SET undone = true "
                    "WHERE season_id = %s AND steam_id = %s", (season_id, sid))
    conn.commit()
    if agg["tiers"]:
        flash(f"Upgrades reset \u2014 {agg['tiers']} tiers removed, "
              f"{agg['refund']} cr are back on the driver's balance. "
              "The in-game car keeps the old build until the next session "
              "prep (or a manual 'Build & assign').", "success")
    else:
        flash("Driver had no upgrades to reset.", "warning")
    return redirect(url_for("career.admin"))


@career_bp.route("/admin/season", methods=["POST"])
@_admin_required
def admin_create_season():
    if not _csrf_ok():
        abort(403)
    f = request.form
    try:
        name = f["name"].strip()
        base_name = f["base_vehicle_name"].strip()
        base_veh = f["base_vehicle_veh"].strip()
        start_credits = int(f["start_credits"])
        credit_first = int(f["credit_first"])
        credit_last = int(f["credit_last"])
        if not name or not base_name or not base_veh:
            raise ValueError("missing fields")
        if credit_last < credit_first:
            flash("credit_last should be >= credit_first (slower earns more).",
                  "warning")
    except (KeyError, ValueError):
        flash("Invalid season form.", "danger")
        return redirect(url_for("career.admin"))

    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO career.seasons "
            "(name, base_vehicle_name, base_vehicle_veh, start_credits, "
            " credit_first, credit_last, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,'draft') RETURNING id",
            (name, base_name, base_veh, start_credits, credit_first, credit_last))
        season_id = cur.fetchone()[0]
        for axis in AXES:
            cur.execute(
                "INSERT INTO career.upgrade_axes "
                "(season_id, axis, base_value, step_per_tier, max_tier, cost_per_tier) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (season_id, axis,
                 float(f.get(f"{axis}_base", 0) or 0),
                 float(f.get(f"{axis}_step", 0) or 0),
                 int(f.get(f"{axis}_max", 5) or 5),
                 int(f.get(f"{axis}_cost", 100) or 100)))
    conn.commit()
    flash(f"Season '{name}' created as draft.", "success")
    return redirect(url_for("career.admin"))


@career_bp.route("/admin/season/<int:season_id>/activate", methods=["POST"])
@_admin_required
def admin_activate(season_id):
    if not _csrf_ok():
        abort(403)
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        # only one active season: finish the current one first
        cur.execute("UPDATE career.seasons SET status='finished', finished_at=now() "
                    "WHERE status='active'")
        cur.execute("UPDATE career.seasons SET status='active', activated_at=now(), "
                    "finished_at=NULL WHERE id=%s", (season_id,))
    conn.commit()
    flash("Season activated.", "success")
    return redirect(url_for("career.admin"))


@career_bp.route("/admin/season/<int:season_id>/finish", methods=["POST"])
@_admin_required
def admin_finish(season_id):
    if not _csrf_ok():
        abort(403)
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE career.seasons SET status='finished', finished_at=now() "
                    "WHERE id=%s", (season_id,))
    conn.commit()
    flash("Season finished.", "success")
    return redirect(url_for("career.admin"))


# --------------------------------------------------- beta participant allowlist
@career_bp.route("/admin/participants/add", methods=["POST"])
@_admin_required
def admin_add_participant():
    if not _csrf_ok():
        abort(403)
    raw = request.form.get("steam_id", "").strip()
    note = request.form.get("note", "").strip() or None
    if not raw.isdigit() or len(raw) < 17:
        flash("Enter a valid SteamID64 (17 digits).", "danger")
        return redirect(url_for("career.admin"))
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO career.allowed_participants (steam_id, note, added_by) "
                    "VALUES (%s, %s, %s) ON CONFLICT (steam_id) DO UPDATE SET note=EXCLUDED.note",
                    (int(raw), note, g.current_steam_id))
    conn.commit()
    flash("Participant added to the beta.", "success")
    return redirect(url_for("career.admin"))


@career_bp.route("/admin/participants/remove", methods=["POST"])
@_admin_required
def admin_remove_participant():
    if not _csrf_ok():
        abort(403)
    raw = request.form.get("steam_id", "").strip()
    if not raw.isdigit():
        abort(400)
    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM career.allowed_participants WHERE steam_id = %s",
                    (int(raw),))
    conn.commit()
    flash("Participant removed from the beta.", "success")
    return redirect(url_for("career.admin"))


# ----------------------------------------------- delete season (two confirmations)
@career_bp.route("/admin/season/<int:season_id>/delete", methods=["POST"])
@_admin_required
def admin_delete_season(season_id):
    """First confirmation: show a dedicated page that requires typing the name."""
    if not _csrf_ok():
        abort(403)
    with _cur() as cur:
        cur.execute("SELECT * FROM career.seasons WHERE id = %s", (season_id,))
        season = cur.fetchone()
        if not season:
            abort(404)
        cur.execute("SELECT COUNT(*) AS n FROM career.enrollments WHERE season_id=%s",
                    (season_id,))
        enrolled = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM career.race_rewards WHERE season_id=%s",
                    (season_id,))
        rewards = cur.fetchone()["n"]
    return render_template("career/admin_delete.html", season=season,
                           enrolled=enrolled, rewards=rewards)


@career_bp.route("/admin/season/<int:season_id>/delete/confirm", methods=["POST"])
@_admin_required
def admin_delete_season_confirm(season_id):
    """Second confirmation: the typed name must match exactly."""
    if not _csrf_ok():
        abort(403)
    typed = request.form.get("confirm_name", "")
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT name FROM career.seasons WHERE id = %s", (season_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
        if typed != row["name"]:
            flash("The typed name did not match — season was NOT deleted.", "danger")
            return redirect(url_for("career.admin"))
        # cascades: upgrade_axes, enrollments, driver_upgrades, race_rewards
        cur.execute("DELETE FROM career.seasons WHERE id = %s", (season_id,))
    conn.commit()
    flash(f"Season '{row['name']}' was permanently deleted.", "success")
    return redirect(url_for("career.admin"))
