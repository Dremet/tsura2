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
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
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
    "events": {
        "label": "Event Server",
        "color": "#6f42c1",
        "description": "Upload cars, tracks and camera settings for the "
                       "manually hosted #1 Event Server.",
    },
}

# Unix account behind each game server.
SERVER_UNIX_USER = {
    "tripleheat": "tripleheat",
    "casual_heat": "heat",
    "hotlapping": "hotlapping",
    "events": "events",
    "career": "career",
}

# Where each game server lives on disk (uploads go to server/config/<subdir>).
SERVER_HOME = {s: f"/home/{u}" for s, u in SERVER_UNIX_USER.items()}

# Servers that take file uploads via the panel (career cars are generated).
UPLOAD_SERVERS = ("tripleheat", "casual_heat", "hotlapping", "events")

# Server-control actions -> script in the game user's home (run via a
# narrow sudoers rule in /etc/sudoers.d/tsura-server-admin).
ACTIONS = {"restart": "restart_server.sh", "update": "update_and_restart.sh"}
ACTION_LOG_DIR = "/srv/tsura/server_config/logs"

PANEL_ENDPOINT = {
    "tripleheat": "admin.tripleheat",
    "casual_heat": "admin.casual_heat",
    "hotlapping": "admin.hotlapping",
    "events": "admin.events",
}

UPLOAD_KINDS = {
    "vehicle": {"subdir": "Vehicles", "ext": ".veh", "magic": b"PK", "label": "car"},
    "track": {"subdir": "Levels", "ext": ".lvl", "magic": None, "label": "track"},
}

CAMERA_PATH = "/home/events/server/config/camera.json"
BACKUP_DIR = "/srv/tsura/server_config/backups"


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


def _form_str(name: str, default: str) -> str:
    return request.form.get(name, "").strip() or default


def _parse_points(text: str, what: str) -> list:
    """Comma/space separated points table, P1 first, 1-20 entries."""
    vals = [v for v in re.split(r"[,\s]+", text.strip()) if v]
    try:
        pts = [int(v) for v in vals]
    except ValueError:
        raise ValueError(f"{what}: whole numbers separated by commas")
    if not pts or len(pts) > 20:
        raise ValueError(f"{what}: between 1 and 20 values")
    return pts


def _point_commands(points) -> list:
    points = list(points)[:20]
    cmds = [f"/points.position{i} = {p}" for i, p in enumerate(points, 1)]
    cmds += [f"/points.position{i} = 0" for i in range(len(points) + 1, 21)]
    return cmds


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


# ---------------------------------------------------------------- uploads
def _safe_filename(raw: str) -> str:
    """Basename only; game files legitimately contain spaces/umlauts/'."""
    name = raw.replace("\\", "/").split("/")[-1].strip()
    if not name or name.startswith(".") or any(ord(c) < 32 for c in name):
        raise ValueError(f"Invalid file name: {raw!r}")
    return name


def _upload_dir(server: str, kind: str) -> str:
    return os.path.join(
        SERVER_HOME[server], "server", "config", UPLOAD_KINDS[kind]["subdir"])


def _recent_files(server: str, kind: str, n: int = 8) -> list:
    try:
        entries = [
            (e.name, e.stat().st_mtime)
            for e in os.scandir(_upload_dir(server, kind))
            if e.is_file() and not e.name.startswith(".")
        ]
    except OSError:
        return []
    entries.sort(key=lambda t: t[1], reverse=True)
    return [
        {"name": name,
         "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")}
        for name, mtime in entries[:n]
    ]


def _upload_context(server: str) -> dict:
    return {
        "upload_server": server,
        "recent_uploads": {k: _recent_files(server, k) for k in UPLOAD_KINDS},
    }


# --------------------------------------------------------- server control
def _server_running(server: str) -> bool:
    try:
        return (
            subprocess.run(
                ["pgrep", "-u", SERVER_UNIX_USER[server], "-x", "TSUs.x86_64"],
                stdout=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _control_context(server: str) -> dict:
    return {"control_server": server, "server_up": _server_running(server)}


def _apply_ingame_admins_now(server: str, admins) -> str:
    """Push a changed in-game admin list to the RUNNING server.

    Writes an autorun.src containing only /admins commands — this does NOT
    touch the running event or session, races continue undisturbed.
    Returns a human-readable note about what happened.
    """
    fallback = "they take effect at the next session start"
    try:
        scripts_dir = os.path.join(SERVER_HOME[server], "server", "config", "Scripts")
        autorun = os.path.join(scripts_dir, "autorun.src")
        if not _server_running(server):
            return f"server is offline — {fallback}"
        if os.path.exists(autorun):
            return f"server is busy with another script — {fallback}"
        commands = ["/admins /clear"]
        commands += [f"/admins /add {sid}" for sid, _label in admins]
        fd, tmp = tempfile.mkstemp(dir=scripts_dir, prefix=".admins.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(commands) + "\n")
            os.chmod(tmp, 0o664)
            os.replace(tmp, autorun)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return "applied to the running server (current session is not interrupted)"
    except OSError as exc:
        return f"could not reach the server ({exc}) — {fallback}"


# ----------------------------------------------------- content validation
VEH_NAME_CACHE = "/srv/tsura/server_config/.veh_names.{server}.json"
# world-readable copy of the vehicle tools (same one the career blueprint uses)
VEH_TOOLS_DIR = "/home/career/career_tools"


def _server_veh_names(server: str) -> set:
    """Vehicle names parsed from the server's .veh files (cached by
    mtime+size). Incomplete: built-in game vehicles have no file and some
    modded .veh don't parse — callers must union other name sources."""
    try:
        if VEH_TOOLS_DIR not in __import__("sys").path:
            __import__("sys").path.insert(0, VEH_TOOLS_DIR)
        import tsu_veh
        cache_path = VEH_NAME_CACHE.format(server=server)
        cache = _load_json(cache_path) or {}
        vdir = _upload_dir(server, "vehicle")
        out, new_cache, dirty = set(), {}, False
        for entry in os.scandir(vdir):
            if not entry.is_file() or not entry.name.lower().endswith(".veh"):
                continue
            st = entry.stat()
            key = f"{st.st_mtime_ns}:{st.st_size}"
            cached = cache.get(entry.name)
            if cached and cached.get("key") == key:
                name = cached.get("name")
            else:
                dirty = True
                try:
                    veh = tsu_veh.read_veh(entry.path)
                    name = veh.get("name") if isinstance(veh, dict) else None
                except Exception:
                    name = None
                cached = {"key": key, "name": name}
            new_cache[entry.name] = cached
            if cached.get("name"):
                out.add(cached["name"])
        if dirty or len(new_cache) != len(cache):
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(new_cache, fh)
            os.chmod(cache_path, 0o664)
        return out
    except Exception:
        return set()


def _check_content_names(kind: str, names, server: str, extra_known=()) -> None:
    """Raise ValueError if a name is unknown on the TSURA servers.

    Known = every track/car ever driven on any TSURA server (DB) + names
    parsed from the server's .veh files + names in the currently saved
    config (extra_known). Case-only mismatches get a spelling suggestion.
    Can be skipped with the 'allow_new' checkbox for freshly uploaded
    content the servers have never seen.
    """
    if request.form.get("allow_new") == "1":
        return
    known = set(_known_tracks() if kind == "track" else _known_vehicles())
    if kind == "vehicle" and server in UPLOAD_SERVERS:
        known |= _server_veh_names(server)
    known |= set(extra_known)
    by_lower = {k.lower(): k for k in known}
    problems = []
    for n in names:
        if n in known:
            continue
        suggestion = by_lower.get(n.lower())
        if suggestion:
            problems.append(f"'{n}' — did you mean '{suggestion}'?")
        else:
            problems.append(f"'{n}'")
    if problems:
        label = "track" if kind == "track" else "car"
        raise ValueError(
            f"Unknown {label} name(s): " + "; ".join(problems) +
            ". Check the exact in-game spelling — or tick “Allow new names” "
            "if this is freshly uploaded content."
        )


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
        "events": url_for("admin.events"),
    }
    # who is admin where (shown to every admin; owner is implicit everywhere)
    overview = {s: [] for s in SERVERS}
    try:
        with _cur() as cur:
            cur.execute(
                "SELECT a.server, a.steam_id, p.driver_name"
                "  FROM webadmin.server_admins a"
                "  LEFT JOIN mart.v_driver_profile p USING (steam_id)"
                " ORDER BY a.server, p.driver_name NULLS LAST, a.steam_id"
            )
            for r in cur.fetchall():
                overview.setdefault(r["server"], []).append(r)
    except Exception:
        pass
    return render_template(
        "admin/index.html",
        servers=servers,
        meta=SERVERS,
        links=links,
        overview=overview,
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
            old_tracks = [t for t, _w in cfg.get("tracks", [])]
            old_cars = (cfg.get("vehicles", []) if server == "tripleheat"
                        else [c for c, _w in cfg.get("cars", [])])
            cfg["number_tracks"] = _form_int("number_tracks", "Tracks per session", 1, 20)
            cfg["tracks"] = _parse_weighted(request.form.get("tracks", ""), "Tracks")
            if server == "tripleheat":
                cfg["vehicles"] = _parse_names(request.form.get("vehicles", ""), "Cars")
                new_cars = cfg["vehicles"]
            else:
                cfg["cars"] = _parse_weighted(request.form.get("cars", ""), "Cars")
                new_cars = [c for c, _w in cfg["cars"]]
            _check_content_names("track", [t for t, _w in cfg["tracks"]],
                                 server, extra_known=old_tracks)
            _check_content_names("vehicle", new_cars, server, extra_known=old_cars)
            cfg["quali"] = {
                "laps": _form_int("quali_laps", "Quali laps", 1, 100),
                "max_minutes": _form_num("quali_max_minutes", "Quali max minutes", 1, 1000),
                "start_style": _form_str("quali_start_style", "Countdown"),
                "contact_rules": _form_str("quali_contact_rules", "EqualGhosts"),
                "points": _parse_points(
                    request.form.get("quali_points", "") or "3, 2, 1", "Quali points"),
            }
            race = {
                "start_style": _form_str("race_start_style", "Random"),
                "contact_rules": _form_str("race_contact_rules", "Normal"),
                "points": _parse_points(
                    request.form.get("race_points", "") or "20, 16, 13, 10, 8, 6, 4, 3, 2, 1",
                    "Race points"),
            }
            if server == "tripleheat":
                race["laps_min"], race["laps_max"] = _form_range("laps", "Race laps", 1, 1000)
            else:
                race["max_laps"] = _form_int("max_laps", "Race max laps", 1, 10000)
                race["max_compounds"] = _form_int("max_compounds", "Max tire compounds", 1, 2)
            race["max_minutes"] = _form_num("race_max_minutes", "Race max minutes", 1, 100000)
            race["fuel_min"], race["fuel_max"] = _form_range("fuel", "Fuel (full-gas time)", 1, 100000)
            race["tires_min"], race["tires_max"] = _form_range("tires", "Tire endurance", 1, 100000)
            cfg["race"] = race
            admins_note = None
            if owner and request.form.get("ingame_admins") is not None:
                old_admins = cfg.get("ingame_admins")
                cfg["ingame_admins"] = _parse_admins(request.form["ingame_admins"])
                if cfg["ingame_admins"] != old_admins:
                    admins_note = _apply_ingame_admins_now(server, cfg["ingame_admins"])
            _save_config(server, cfg)
            msg = f"{meta['label']} config saved — used at the next session start."
            if admins_note:
                msg += f" In-game admins: {admins_note}."
            flash(msg, "success")
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
        **_upload_context(server),
        **_control_context(server),
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
            _check_content_names("track", [track], "hotlapping",
                                 extra_known=[cfg.get("track", "")])
            _check_content_names("vehicle", [vehicle], "hotlapping",
                                 extra_known=[cfg.get("vehicle", "")])
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
        **_upload_context("hotlapping"),
        **_control_context("hotlapping"),
    )


@admin_bp.route("/events")
@_server_admin_required("events")
def events():
    try:
        camera_mtime = datetime.fromtimestamp(
            os.stat(CAMERA_PATH).st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        camera_mtime = None
    return render_template(
        "admin/events.html",
        meta=SERVERS["events"],
        camera_mtime=camera_mtime,
        **_upload_context("events"),
        **_control_context("events"),
    )


# Optional fields on the event-server panel; only filled fields are sent.
EVENT_PUSH_FIELDS = [
    ("max_laps", "/race.maxLaps", "int"),
    ("max_minutes", "/race.maxMinutes", "num"),
    ("start_style", "/race.startStyle", "str"),
    ("starting_order", "/race.startingOrder", "str"),
    ("contact_rules", "/race.ContactRules", "str"),
    ("fuel_on", "/fuel.fuelOn", "bool"),
    ("fuel_full_gas_time", "/fuelFullGasTime", "int"),
    ("tire_wear_on", "/tireWear.tireWearOn", "bool"),
    ("compound_count", "/tireWear.tireCompoundCount", "int"),
    ("compound1_endurance", "/tireWear.compound1Endurance", "int"),
    ("compound2_endurance", "/tireWear.compound2Endurance", "int"),
    ("collision_damage_on", "/damage.collisionDamageOn", "bool"),
    ("drafting_on", "/drafting.draftingOn", "bool"),
]


@admin_bp.route("/events/push", methods=["POST"])
@_server_admin_required("events")
def events_push():
    """One-shot push of race settings to the running event server.

    Nothing re-applies these later — in-game changes made afterwards always
    take precedence (explicitly wanted for the manually hosted server).
    """
    if not _csrf_ok():
        abort(400)
    commands, errors = [], []
    for name, cmd, typ in EVENT_PUSH_FIELDS:
        raw = request.form.get(name, "").strip()
        if not raw:
            continue
        try:
            if typ == "int":
                val = int(raw)
            elif typ == "num":
                v = float(raw)
                val = int(v) if v.is_integer() else v
            elif typ == "bool":
                val = 1 if raw in ("1", "true", "on", "yes") else 0
            else:
                val = raw
        except ValueError:
            errors.append(f"{name}: '{raw}' is not a number")
            continue
        commands.append(f"{cmd} = {val}")
    pts = request.form.get("points", "").strip()
    if pts:
        try:
            commands += _point_commands(_parse_points(pts, "Points"))
        except ValueError as exc:
            errors.append(str(exc))
    for ln, line in enumerate(request.form.get("raw_commands", "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith("/"):
            errors.append(f"Custom command line {ln}: must start with /")
            continue
        commands.append(line)
    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("admin.events"))
    if not commands:
        flash("Nothing to send — all fields were empty.", "warning")
        return redirect(url_for("admin.events"))
    if not _server_running("events"):
        flash("The event server is offline — nothing was sent.", "danger")
        return redirect(url_for("admin.events"))
    scripts_dir = os.path.join(SERVER_HOME["events"], "server", "config", "Scripts")
    autorun = os.path.join(scripts_dir, "autorun.src")
    if os.path.exists(autorun):
        flash("The server is busy with another script — try again in a "
              "few seconds.", "warning")
        return redirect(url_for("admin.events"))
    try:
        fd, tmp = tempfile.mkstemp(dir=scripts_dir, prefix=".push.")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(commands) + "\n")
        os.chmod(tmp, 0o664)
        os.replace(tmp, autorun)
    except OSError as exc:
        flash(f"Could not send commands: {exc}", "danger")
        return redirect(url_for("admin.events"))
    try:
        log_path = os.path.join(ACTION_LOG_DIR, "events.log")
        with open(log_path, "a") as lf:
            lf.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} settings push "
                     f"by {g.current_steam_id}\n")
            lf.write("\n".join(commands) + "\n")
        os.chmod(log_path, 0o664)
    except OSError:
        pass
    flash(f"Sent {len(commands)} command(s) to the event server. In-game "
          "changes made afterwards stay in effect until the next push.",
          "success")
    return redirect(url_for("admin.events"))


@admin_bp.route("/<server>/action/<action>", methods=["POST"])
def server_action(server, action):
    if server not in SERVER_UNIX_USER or action not in ACTIONS:
        abort(404)
    if not is_server_admin(g.get("current_steam_id"), server):
        abort(403)
    if not _csrf_ok():
        abort(400)
    user = SERVER_UNIX_USER[server]
    script = os.path.join(SERVER_HOME[server], ACTIONS[action])
    back = redirect(url_for(PANEL_ENDPOINT.get(server, "admin.index"))
                    if server != "career" else url_for("career.admin"))
    try:
        # logging is best-effort — never block the action on it
        lf = None
        try:
            log_path = os.path.join(ACTION_LOG_DIR, f"{server}.log")
            lf = open(log_path, "a")
            os.chmod(log_path, 0o664)
            lf.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} {action} "
                     f"triggered by {g.current_steam_id}\n")
            lf.flush()
        except OSError:
            lf = None
        proc = subprocess.Popen(
            ["sudo", "-n", "-u", user, script],
            stdout=lf if lf else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if lf else subprocess.DEVNULL,
            start_new_session=True,
        )
        if lf:
            lf.close()
        # sudo fails immediately (exit 1) if the sudoers rule is missing
        try:
            rc = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rc = None
        if rc not in (None, 0):
            flash("Could not start the action — server permissions for the "
                  "website are not set up (sudoers).", "danger")
            return back
    except OSError as exc:
        flash(f"Could not start the action: {exc}", "danger")
        return back
    if action == "restart":
        flash(f"{SERVERS[server]['label']} server restart started — the "
              "server is back in about 2 minutes.", "success")
    else:
        flash(f"{SERVERS[server]['label']} update & restart started — this "
              "can take several minutes.", "success")
    return back


@admin_bp.route("/<server>/upload/<kind>", methods=["POST"])
def upload(server, kind):
    if server not in UPLOAD_SERVERS or kind not in UPLOAD_KINDS:
        abort(404)
    if not is_server_admin(g.get("current_steam_id"), server):
        abort(403)
    if not _csrf_ok():
        abort(400)
    spec = UPLOAD_KINDS[kind]
    target_dir = _upload_dir(server, kind)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("No file selected.", "warning")
        return redirect(url_for(PANEL_ENDPOINT[server]))

    saved, errors = [], []
    for f in files:
        try:
            name = _safe_filename(f.filename)
            if not name.lower().endswith(spec["ext"]):
                raise ValueError(f"{name}: must be a {spec['ext']} file")
            if spec["magic"]:
                head = f.stream.read(len(spec["magic"]))
                f.stream.seek(0)
                if head != spec["magic"]:
                    raise ValueError(f"{name}: not a valid {spec['label']} file")
            fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".upload.")
            try:
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(f.stream, out)
                os.chmod(tmp, 0o664)
                os.replace(tmp, os.path.join(target_dir, name))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            saved.append(name)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))

    if saved:
        flash(
            f"Uploaded {len(saved)} {spec['label']} file(s): {', '.join(saved)} — "
            "the running server scans files only at startup: loaded after the "
            "next restart (daily ~5:00, plus 20:45 before sessions) or hit "
            "“Restart server” to use them right away.",
            "success",
        )
    for e in errors:
        flash(e, "danger")
    return redirect(url_for(PANEL_ENDPOINT[server]))


@admin_bp.route("/events/upload/camera", methods=["POST"])
@_server_admin_required("events")
def upload_camera():
    if not _csrf_ok():
        abort(400)
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "warning")
        return redirect(url_for("admin.events"))
    data = f.read()
    try:
        json.loads(data.decode("utf-8-sig"))
    except Exception:
        flash(f"{f.filename}: not a valid JSON file.", "danger")
        return redirect(url_for("admin.events"))
    try:
        backup = os.path.join(
            BACKUP_DIR, f"camera.json.{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(CAMERA_PATH, backup)
        # in-place rewrite: tsura has write access to the file, not the dir
        with open(CAMERA_PATH, "wb") as out:
            out.write(data)
    except OSError as exc:
        flash(f"Could not replace camera.json: {exc}", "danger")
        return redirect(url_for("admin.events"))
    flash("camera.json replaced (backup kept) — active after the next "
          "server restart.", "success")
    return redirect(url_for("admin.events"))


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
