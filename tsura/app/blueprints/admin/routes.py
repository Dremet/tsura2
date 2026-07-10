"""Cross-server admin area for tsura.org.

Per-server panel access is granted via webadmin.server_admins (migration
015). The site owner always has access to everything and is the only one
who may edit admin rights. The TripleHeat / Casual Heat / Hotlapping panels
read and write the JSON files under /srv/tsura/server_config/ which the
game-server scripts consume (create_autorun.py / run_event_init.py /
apply_web_config.py) — every value falls back to the scripts' built-in
defaults if missing, so a bad config can never break a session.
"""
from __future__ import annotations

import hmac
import json
import os
import tempfile
from functools import wraps

import psycopg
from flask import (abort, current_app, flash, g, redirect, render_template,
                   request, url_for)

from . import admin_bp
from ...extensions import db_pool, make_csrf_token

# The only user who may manage admin rights (and the in-game admin lists).
OWNER_STEAM_ID = 76561197989276622

CONFIG_DIR = "/srv/tsura/server_config"

SERVERS = {
    "career": {
        "label": "Career",
        "color": "#ffc107",
        "description": "Seasons, participants, upgrades, penalties and car builds.",
    },
    "tripleheat": {
        "label": "TripleHeat",
        "color": "#dc3545",
        "description": "Car list, track pool and quali/race parameters "
                       "for the Friday sessions.",
    },
    "casual_heat": {
        "label": "Casual Heat",
        "color": "#0d6efd",
        "description": "Car pool, track pool and quali/race parameters "
                       "for the Wednesday sessions.",
    },
    "hotlapping": {
        "label": "Hotlapping",
        "color": "#20c997",
        "description": "Track, car and start-behind distance — applied to "
                       "the live server within a minute.",
    },
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


def is_owner(steam_id) -> bool:
    try:
        return int(steam_id) == OWNER_STEAM_ID
    except (TypeError, ValueError):
        return False


def user_admin_servers(steam_id) -> list:
    """Servers whose admin panel this user may open (all for the owner)."""
    if not steam_id:
        return []
    if is_owner(steam_id):
        return list(SERVERS)
    with _cur() as cur:
        cur.execute(
            "SELECT server FROM webadmin.server_admins WHERE steam_id = %s",
            (steam_id,),
        )
        allowed = {r["server"] for r in cur.fetchall()}
    return [s for s in SERVERS if s in allowed]


def is_server_admin(steam_id, server: str) -> bool:
    return server in user_admin_servers(steam_id)


def _server_admin_required(server):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not is_server_admin(g.get("current_steam_id"), server):
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return deco


def _owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_owner(g.get("current_steam_id")):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------- config I/O
def _config_path(server: str) -> str:
    return os.path.join(CONFIG_DIR, f"{server}.json")


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _load_config(server: str) -> dict:
    return _load_json(_config_path(server)) or {}


def _save_config(server: str, cfg: dict) -> None:
    """Atomic replace — the game servers may read the file at any moment."""
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=f".{server}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o664)
        os.replace(tmp, _config_path(server))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------- form parsing
def _parse_weighted(text: str, what: str) -> list:
    """Lines of 'Name | weight' (weight optional, default 1)."""
    items = []
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        name, _, w = line.partition("|")
        name = name.strip()
        w = w.strip() or "1"
        if not name:
            raise ValueError(f"{what}, line {ln}: empty name")
        try:
            weight = float(w)
        except ValueError:
            raise ValueError(f"{what}, line {ln}: weight '{w}' is not a number")
        if weight < 0:
            raise ValueError(f"{what}, line {ln}: weight must be >= 0")
        items.append([name, weight])
    if not items:
        raise ValueError(f"{what}: at least one entry is required")
    return items


def _parse_names(text: str, what: str) -> list:
    items = [line.strip() for line in text.splitlines() if line.strip()]
    if not items:
        raise ValueError(f"{what}: at least one entry is required")
    return items


def _parse_admins(text: str) -> list:
    """Lines of 'steam_id | label' for the in-game admin list."""
    admins = []
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        sid, _, label = line.partition("|")
        sid = sid.strip()
        if not sid.isdigit():
            raise ValueError(f"In-game admins, line {ln}: '{sid}' is not a Steam ID")
        admins.append([sid, label.strip() or sid])
    if not admins:
        raise ValueError("In-game admins: at least one entry is required")
    return admins


def _form_int(name: str, label: str, lo: int, hi: int) -> int:
    raw = request.form.get(name, "").strip()
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"{label}: '{raw}' is not a whole number")
    if not lo <= val <= hi:
        raise ValueError(f"{label}: must be between {lo} and {hi}")
    return val


def _form_num(name: str, label: str, lo: float, hi: float):
    raw = request.form.get(name, "").strip()
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(f"{label}: '{raw}' is not a number")
    if not lo <= val <= hi:
        raise ValueError(f"{label}: must be between {lo} and {hi}")
    return int(val) if val.is_integer() else val


def _form_range(prefix: str, label: str, lo: int, hi: int) -> tuple:
    a = _form_int(f"{prefix}_min", f"{label} (min)", lo, hi)
    b = _form_int(f"{prefix}_max", f"{label} (max)", lo, hi)
    if a > b:
        raise ValueError(f"{label}: min must not be greater than max")
    return a, b


# ------------------------------------------------------------- datalists
def _known_names(sql: str) -> list:
    try:
        with _cur() as cur:
            cur.execute(sql)
            return [r["name"] for r in cur.fetchall() if r["name"]]
    except Exception:
        return []


def _known_tracks() -> list:
    return _known_names(
        "SELECT DISTINCT track_name AS name FROM mart.v_race_results "
        "UNION SELECT DISTINCT track_name FROM mart.v_hotlap_grouped_sessions "
        "ORDER BY 1"
    )


def _known_vehicles() -> list:
    return _known_names(
        "SELECT DISTINCT vehicle_name AS name FROM mart.v_race_results ORDER BY 1"
    )


def _fmt_weighted(items) -> str:
    return "\n".join(f"{name} | {weight:g}" for name, weight in items)


def _fmt_admins(items) -> str:
    return "\n".join(f"{sid} | {label}" for sid, label in items)


# ---------------------------------------------------------------- routes
@admin_bp.route("/")
def index():
    servers = user_admin_servers(g.get("current_steam_id"))
    if not servers:
        abort(403)
    links = {
        "career": url_for("career.admin"),
        "tripleheat": url_for("admin.tripleheat"),
        "casual_heat": url_for("admin.casual_heat"),
        "hotlapping": url_for("admin.hotlapping"),
    }
    return render_template(
        "admin/index.html",
        servers=servers,
        meta=SERVERS,
        links=links,
        is_owner=is_owner(g.get("current_steam_id")),
    )


def _heat_panel(server: str):
    """Shared panel logic for TripleHeat and Casual Heat."""
    meta = SERVERS[server]
    owner = is_owner(g.get("current_steam_id"))
    endpoint = "admin.tripleheat" if server == "tripleheat" else "admin.casual_heat"

    if request.method == "POST":
        if not _csrf_ok():
            abort(400)
        cfg = _load_config(server)
        try:
            cfg["number_tracks"] = _form_int("number_tracks", "Tracks per session", 1, 20)
            cfg["tracks"] = _parse_weighted(request.form.get("tracks", ""), "Tracks")
            if server == "tripleheat":
                cfg["vehicles"] = _parse_names(request.form.get("vehicles", ""), "Cars")
            else:
                cfg["cars"] = _parse_weighted(request.form.get("cars", ""), "Cars")
            cfg["quali"] = {
                "laps": _form_int("quali_laps", "Quali laps", 1, 100),
                "max_minutes": _form_num("quali_max_minutes", "Quali max minutes", 1, 1000),
            }
            race = {}
            if server == "tripleheat":
                race["laps_min"], race["laps_max"] = _form_range("laps", "Race laps", 1, 1000)
            else:
                race["max_laps"] = _form_int("max_laps", "Race max laps", 1, 10000)
                race["max_compounds"] = _form_int("max_compounds", "Max tire compounds", 1, 2)
            race["max_minutes"] = _form_num("race_max_minutes", "Race max minutes", 1, 100000)
            race["fuel_min"], race["fuel_max"] = _form_range("fuel", "Fuel (full-gas time)", 1, 100000)
            race["tires_min"], race["tires_max"] = _form_range("tires", "Tire endurance", 1, 100000)
            cfg["race"] = race
            if owner and request.form.get("ingame_admins") is not None:
                cfg["ingame_admins"] = _parse_admins(request.form["ingame_admins"])
            _save_config(server, cfg)
            flash(f"{meta['label']} config saved — used at the next session start.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except OSError as exc:
            flash(f"Could not write config: {exc}", "danger")
        return redirect(url_for(endpoint))

    cfg = _load_config(server)
    return render_template(
        "admin/heat_config.html",
        server=server,
        meta=meta,
        endpoint=endpoint,
        cfg=cfg,
        tracks_text=_fmt_weighted(cfg.get("tracks", [])),
        cars_text=_fmt_weighted(cfg.get("cars", [])) if server == "casual_heat" else "",
        vehicles_text="\n".join(cfg.get("vehicles", [])) if server == "tripleheat" else "",
        admins_text=_fmt_admins(cfg.get("ingame_admins", [])),
        is_owner=owner,
        track_options=_known_tracks(),
        vehicle_options=_known_vehicles(),
    )


@admin_bp.route("/tripleheat", methods=["GET", "POST"])
@_server_admin_required("tripleheat")
def tripleheat():
    return _heat_panel("tripleheat")


@admin_bp.route("/casual-heat", methods=["GET", "POST"])
@_server_admin_required("casual_heat")
def casual_heat():
    return _heat_panel("casual_heat")


@admin_bp.route("/hotlapping", methods=["GET", "POST"])
@_server_admin_required("hotlapping")
def hotlapping():
    if request.method == "POST":
        if not _csrf_ok():
            abort(400)
        cfg = _load_config("hotlapping")
        try:
            track = request.form.get("track", "").strip()
            vehicle = request.form.get("vehicle", "").strip()
            if not track or not vehicle:
                raise ValueError("Track and car are both required")
            for name in (track, vehicle):
                if "'" in name and '"' in name:
                    raise ValueError(
                        f"'{name}' contains both quote characters — "
                        "the game console cannot quote that name"
                    )
            cfg["track"] = track
            cfg["vehicle"] = vehicle
            cfg["hotlap_behind_distance"] = _form_int(
                "hotlap_behind_distance", "Start-behind distance", 0, 100000)
            cfg["events_per_session"] = _form_int(
                "events_per_session", "Events per session", 1, 20)
            _save_config("hotlapping", cfg)
            flash("Hotlapping config saved — the server applies it within "
                  "about a minute.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except OSError as exc:
            flash(f"Could not write config: {exc}", "danger")
        return redirect(url_for("admin.hotlapping"))

    cfg = _load_config("hotlapping")
    applied = _load_json(os.path.join(CONFIG_DIR, "hotlapping.applied.json"))
    return render_template(
        "admin/hotlapping.html",
        meta=SERVERS["hotlapping"],
        cfg=cfg,
        pending=(applied != cfg),
        track_options=_known_tracks(),
        vehicle_options=_known_vehicles(),
    )


# ---------------------------------------------------------- admin rights
@admin_bp.route("/admins")
@_owner_required
def admins():
    with _cur() as cur:
        cur.execute(
            "SELECT a.server, a.steam_id, a.note, a.added_at, p.driver_name"
            "  FROM webadmin.server_admins a"
            "  LEFT JOIN mart.v_driver_profile p USING (steam_id)"
            " ORDER BY a.server, p.driver_name NULLS LAST, a.steam_id"
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT driver_name, steam_id FROM mart.v_driver_profile"
            " WHERE driver_name IS NOT NULL ORDER BY lower(driver_name)"
        )
        drivers = cur.fetchall()
    by_server = {s: [] for s in SERVERS}
    for r in rows:
        by_server.setdefault(r["server"], []).append(r)
    return render_template(
        "admin/admins.html",
        meta=SERVERS,
        by_server=by_server,
        drivers=drivers,
    )


def _resolve_user(ident: str):
    """Resolve a tsura.org username (or raw Steam ID) to (steam_id, name)."""
    ident = ident.strip()
    if not ident:
        raise ValueError("Please enter a username")
    with _cur() as cur:
        if ident.isdigit() and len(ident) == 17:
            cur.execute(
                "SELECT steam_id, driver_name FROM mart.v_driver_profile"
                " WHERE steam_id = %s", (int(ident),))
            row = cur.fetchone()
            return (int(ident), row["driver_name"] if row else None)
        cur.execute(
            "SELECT steam_id, driver_name FROM mart.v_driver_profile"
            " WHERE lower(driver_name) = lower(%s)", (ident,))
        rows = cur.fetchall()
    if not rows:
        raise ValueError(f"No tsura.org user named '{ident}' found")
    if len(rows) > 1:
        ids = ", ".join(str(r["steam_id"]) for r in rows)
        raise ValueError(
            f"Several users are named '{ident}' ({ids}) — "
            "please enter the Steam ID instead")
    return rows[0]["steam_id"], rows[0]["driver_name"]


@admin_bp.route("/admins/add", methods=["POST"])
@_owner_required
def admins_add():
    if not _csrf_ok():
        abort(400)
    server = request.form.get("server", "")
    if server not in SERVERS:
        abort(400)
    try:
        steam_id, name = _resolve_user(request.form.get("user", ""))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin.admins"))
    with _cur() as cur:
        cur.execute(
            "INSERT INTO webadmin.server_admins (server, steam_id, note, added_by)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (server, steam_id, name, g.current_steam_id),
        )
        cur.connection.commit()
    flash(f"{name or steam_id} is now a {SERVERS[server]['label']} admin.", "success")
    return redirect(url_for("admin.admins"))


@admin_bp.route("/admins/remove", methods=["POST"])
@_owner_required
def admins_remove():
    if not _csrf_ok():
        abort(400)
    server = request.form.get("server", "")
    if server not in SERVERS:
        abort(400)
    try:
        steam_id = int(request.form.get("steam_id", ""))
    except ValueError:
        abort(400)
    with _cur() as cur:
        cur.execute(
            "DELETE FROM webadmin.server_admins WHERE server = %s AND steam_id = %s",
            (server, steam_id),
        )
        cur.connection.commit()
    flash("Admin removed.", "success")
    return redirect(url_for("admin.admins"))
