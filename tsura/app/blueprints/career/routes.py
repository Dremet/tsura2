"""Career routes: public displays + logged-in car tuning + admin season mgmt.

Reads career.* config tables and mart.v_career_* views (SELECT granted to the
`tsura` role); writes seasons/enrollments/upgrades to career.* (write granted).
Race results + rewards are produced by the pipeline and read via mart views.
"""
from __future__ import annotations

import hmac
from functools import wraps

import psycopg
from flask import (abort, current_app, flash, g, redirect, render_template,
                   request, url_for)

from . import career_bp
from ...extensions import db_pool, make_csrf_token

AXES = ["top_speed", "acceleration", "braking", "downforce"]
AXIS_LABELS = {
    "top_speed": "Top Speed",
    "acceleration": "Acceleration",
    "braking": "Braking",
    "downforce": "Downforce",
}


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
            standings = cur.fetchall()
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
                           is_admin=_is_admin(g.get("current_steam_id")))


@career_bp.route("/standings")
def standings():
    with _cur() as cur:
        seasons = _seasons(cur)
        sel = request.args.get("season", type=int)
        season = next((s for s in seasons if s["id"] == sel), None) \
            or _active_season(cur) or (seasons[0] if seasons else None)
        rows = []
        if season:
            cur.execute(
                "SELECT * FROM mart.v_career_standings WHERE season_id = %s "
                "ORDER BY points_total DESC, wins DESC", (season["id"],))
            rows = cur.fetchall()
    return render_template("career/standings.html", seasons=seasons,
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
            per_driver = {}
            for r in cur.fetchall():
                d = per_driver.setdefault(
                    r["steam_id"],
                    {"driver_name": r["driver_name"], "steam_id": r["steam_id"],
                     "axes": {}})
                d["axes"][r["axis"]] = r
            table = list(per_driver.values())
    return render_template("career/upgrades.html", seasons=seasons, season=season,
                           table=table, axes=AXES, axis_labels=AXIS_LABELS,
                           axes_cfg=axes_cfg)


@career_bp.route("/results")
def results():
    with _cur() as cur:
        cur.execute("""
            SELECT r.session_id, r.utc_start_time, r.track_name, r.season_id,
                   MIN(r.participant_count) AS participants,
                   MIN(r.driver_name) FILTER (WHERE r.position = 1) AS winner
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
        items = []
        for axis in AXES:
            c = cfg.get(axis)
            if not c:
                continue
            tier = tiers.get(axis, 0)
            items.append({
                "axis": axis, "label": AXIS_LABELS[axis], "tier": tier,
                "max_tier": c["max_tier"], "cost": c["cost_per_tier"],
                "base_value": c["base_value"], "step": c["step_per_tier"],
                "current_value": c["base_value"] + tier * c["step_per_tier"],
                "next_value": c["base_value"] + (tier + 1) * c["step_per_tier"],
                "can_buy": tier < c["max_tier"] and balance >= c["cost_per_tier"],
                "maxed": tier >= c["max_tier"],
            })
    return render_template("career/garage.html", season=season, enrolled=True,
                           balance=balance, items=items)


@career_bp.route("/join", methods=["POST"])
@_login_required
def join():
    if not _csrf_ok():
        abort(403)
    sid = g.current_steam_id
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
            flash(f"{AXIS_LABELS[axis]} upgraded!", "success")
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
    return render_template("career/admin.html", seasons=seasons, axes=AXES,
                           axis_labels=AXIS_LABELS)


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
