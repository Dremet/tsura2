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
from .content_files import read_content_file
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
    "topdown": {
        "label": "TopDown",
        "color": "#fd7e14",
        "description": "Track and car pool, laps, bots and cameras for the "
                       "automatic top-down heats.",
    },
}

# Unix account behind each game server.
SERVER_UNIX_USER = {
    "tripleheat": "tripleheat",
    "casual_heat": "heat",
    "hotlapping": "hotlapping",
    "events": "events",
    "career": "career",
    "topdown": "topdown",
}

# Where each game server lives on disk (uploads go to server/config/<subdir>).
SERVER_HOME = {s: f"/home/{u}" for s, u in SERVER_UNIX_USER.items()}

# Servers that take file uploads via the panel (career cars are generated).
UPLOAD_SERVERS = ("tripleheat", "casual_heat", "hotlapping", "events", "topdown")

# Server-control actions -> script in the game user's home (run via a
# narrow sudoers rule in /etc/sudoers.d/tsura-server-admin).
ACTIONS = {"restart": "restart_server.sh", "update": "update_and_restart.sh"}
ACTION_LOG_DIR = "/srv/tsura/server_config/logs"

PANEL_ENDPOINT = {
    "tripleheat": "admin.tripleheat",
    "casual_heat": "admin.casual_heat",
    "hotlapping": "admin.hotlapping",
    "events": "admin.events",
    "topdown": "admin.topdown",
}

UPLOAD_KINDS = {
    "vehicle": {"subdir": "Vehicles", "ext": ".veh", "magic": b"PK", "label": "car"},
    "track": {"subdir": "Levels", "ext": ".lvl", "magic": None, "label": "track"},
    # AI driving lines, one file per track/car pair. Only topdown offers these:
    # in-game they can be uploaded during session init only, and its controller
    # starts a heat 30s after someone joins, so that window is unusable there.
    "ai_line": {"subdir": "AI", "ext": ".aid", "magic": None,
                "label": "AI line"},
}

# Which upload boxes a panel shows. AI lines are a topdown-only concept.
SERVER_UPLOAD_KINDS = {s: ("vehicle", "track") for s in UPLOAD_SERVERS}
SERVER_UPLOAD_KINDS["topdown"] = ("vehicle", "track", "ai_line")

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


def _known_tracks(server: str | None = None) -> list:
    """Track names for a datalist: raced anywhere, plus installed on `server`."""
    names = set(_known_names(
        "SELECT DISTINCT track_name AS name FROM mart.v_race_results "
        "UNION SELECT DISTINCT track_name FROM mart.v_hotlap_grouped_sessions "
        "ORDER BY 1"
    ))
    if server:
        names |= _installed_names(server, "track")
    return sorted(names, key=str.lower)


def _known_vehicles(server: str | None = None) -> list:
    names = set(_known_names(
        "SELECT DISTINCT vehicle_name AS name FROM mart.v_race_results ORDER BY 1"
    ))
    if server:
        names |= _installed_names(server, "vehicle")
    return sorted(names, key=str.lower)


def _fmt_weighted(items) -> str:
    return "\n".join(f"{name} | {weight:g}" for name, weight in items)


# -------------------------------------------------- advanced event params
# All in-game event settings are shown pre-filled with the server's CURRENT
# values (game.json eventSettings, i.e. the state loaded at boot) and are
# overridable per quali/race. Only values that DIFFER from the current
# default are stored and sent — so the pre-filled form is a no-op until
# something is actually changed. host/port/password live outside
# eventSettings and are never exposed.
PARAM_SKIP_PREFIXES = ()

# values the scripts randomize per race — overriding them disables that
RANDOMIZED_PATHS = {
    "tripleheat": {"race.maxLaps", "fuel.fuelFullGasTime",
                   "tireWear.compound1Endurance"},
    "casual_heat": {"fuel.fuelFullGasTime", "tireWear.tireCompoundCount",
                    "tireWear.compound1Endurance",
                    "tireWear.compound2Endurance"},
}


def _points_defaults(points):
    points = list(points)[:20]
    out = {f"race.points.position{i}": p for i, p in enumerate(points, 1)}
    out.update({f"race.points.position{i}": 0
                for i in range(len(points) + 1, 21)})
    return out


# What the event-init scripts set per branch — these are the true "current"
# values for the quali/race columns (game.json only reflects the boot state).
SCRIPT_EVENT_DEFAULTS = {
    "tripleheat": {
        "quali": {"race.maxLaps": 1, "race.maxMinutes": 3,
                  "fuel.fuelOn": 0, "tireWear.tireWearOn": 0,
                  **_points_defaults([3, 2, 1])},
        "race": {"race.maxMinutes": 1440, "fuel.fuelOn": 1,
                 "tireWear.tireWearOn": 1,
                 "tireWear.tireWearOversteeringEffect": 5,
                 "drafting.draftingSpeedEffect": 5,
                 "drafting.maxDraftingDistance": 45,
                 "drafting.maxDraftingAngle": 20,
                 "drafting.draftingDownforceReduction": 12,
                 "drafting.draftingForMaximumEffect": 90,
                 "drafting.draftingAttenuationPower": 1.5,
                 **_points_defaults([20, 16, 13, 10, 8, 6, 4, 3, 2, 1])},
    },
    "casual_heat": {
        "quali": {"race.maxLaps": 2, "race.maxMinutes": 5,
                  "fuel.fuelOn": 0, "tireWear.tireWearOn": 0,
                  **_points_defaults([3, 2, 1])},
        "race": {"race.maxLaps": 500, "race.maxMinutes": 8,
                 "fuel.fuelOn": 1, "tireWear.tireWearOn": 1,
                 **_points_defaults([20, 16, 13, 10, 8, 6, 4, 3, 2, 1])},
    },
}


def _column_defaults(server: str, base: dict, branch: str) -> dict:
    d = dict(base)
    d.update(SCRIPT_EVENT_DEFAULTS.get(server, {}).get(branch, {}))
    return d


def _fmt_default(val):
    if isinstance(val, float):
        return f"{val:g}"
    return str(val)


def _event_param_specs(server: str) -> list:
    """[(section, [(path, default, display), ...]), ...] — current values
    from the server's game.json (fallback: Scripts/eventsettings.json)."""
    tree = None
    for candidate in (
        os.path.join(SERVER_HOME[server], "server", "config", "game.json"),
        os.path.join(SERVER_HOME[server], "server", "config",
                     "Scripts", "eventsettings.json"),
    ):
        try:
            with open(candidate, encoding="utf-8-sig") as fh:
                tree = json.load(fh)
            if "eventSettings" in tree:
                tree = tree["eventSettings"]
            break
        except Exception:
            continue
    if not isinstance(tree, dict):
        return []
    sections = []
    for section, body in tree.items():
        if not isinstance(body, dict):
            continue
        fields = []
        def walk(obj, prefix):
            for key, val in obj.items():
                p = f"{prefix}.{key}"
                if any(p.startswith(s) for s in PARAM_SKIP_PREFIXES):
                    continue
                if isinstance(val, dict):
                    walk(val, p)
                elif isinstance(val, (int, float, bool)):
                    d = int(val) if isinstance(val, bool) else val
                    fields.append((p, d, _fmt_default(d)))
        walk(body, section)
        if fields:
            sections.append((section, fields))
    return sections


def _approx_equal(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return a == b


def _parse_param_overrides(prefix: str, defaults: dict, what: str) -> dict:
    """Read '<prefix><param.path>' fields; keep only values that differ
    from the server's current default."""
    out = {}
    for path, default in defaults.items():
        raw = request.form.get(prefix + path, "").strip()
        if not raw:
            continue
        low = raw.lower()
        if low in ("true", "on"):
            val = 1
        elif low in ("false", "off"):
            val = 0
        else:
            try:
                f = float(raw)
                val = int(f) if f.is_integer() else f
            except ValueError:
                raise ValueError(
                    f"{what} parameter {path}: '{raw}' is not a number "
                    "(use 1/0 for on/off)")
        if _approx_equal(val, default):
            continue
        out[path] = val
    return out


# ------------------------------------------------- vehicle collision
# One global block per server, applied to every car. The three values only
# bite while physics.adjustVehicleCollisionSettings is on, so the panel keeps
# them behind a single "enabled" switch and the session scripts always emit
# the master switch with them.
COLLISION_FIELDS = [
    ("collisionSteadiness", "Collision steadiness",
     "How hard a car resists being knocked off line."),
    ("collisionExtraStability", "Collision extra stability",
     "Extra damping on top of the steadiness."),
    ("stuckAvoidance", "Stuck avoidance",
     "How eagerly the game untangles cars that jam into each other."),
]


def _collision_defaults(server: str) -> dict:
    """The server's own physics values, used as form placeholders."""
    wanted = {key for key, _label, _help in COLLISION_FIELDS}
    return {path.split(".")[-1]: default
            for _sec, fields in _event_param_specs(server)
            for path, default, _disp in fields
            if path.startswith("physics.") and path.split(".")[-1] in wanted}


def _collision_from_form(old: dict) -> dict:
    """The posted collision block, or {} when the box is unticked.

    An unticked box still stores a disabled block, because the scripts need
    to actively switch the game's own collision handling back on.
    """
    enabled = request.form.get("collision_enabled") == "1"
    block = {"enabled": enabled}
    for key, label, _help in COLLISION_FIELDS:
        raw = request.form.get(f"collision_{key}", "").strip()
        if not raw:
            if not enabled:
                continue
            raise ValueError(f"{label} is required while the collision "
                             f"settings are switched on")
        block[key] = _form_num(f"collision_{key}", label, 0, 100)
    # A server that never used the block keeps a clean config: only store a
    # disabled block once there is something to switch back off.
    return {} if not enabled and not old else block


def _store_collision(cfg: dict) -> None:
    """Write the posted block into `cfg`, or drop the key when unused."""
    block = _collision_from_form(cfg.get("collision"))
    if block:
        cfg["collision"] = block
    else:
        cfg.pop("collision", None)


def _collision_context(server: str, cfg: dict) -> dict:
    return {
        "collision_fields": COLLISION_FIELDS,
        "collision": cfg.get("collision") or {},
        "collision_defaults": _collision_defaults(server),
    }


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
    kinds = SERVER_UPLOAD_KINDS.get(server, ("vehicle", "track"))
    return {
        "upload_server": server,
        "upload_kinds": kinds,
        "upload_labels": {k: UPLOAD_KINDS[k] for k in kinds},
        "recent_uploads": {k: _recent_files(server, k) for k in kinds},
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


def _sync_ingame_admins() -> None:
    """The web admin lists ARE the in-game admin lists.

    Mirror webadmin.server_admins (owner always first) into every server's
    config JSON — read by the session-start scripts (TH/CH), the hotlapping
    applier and career's create_autorun — and push the new list to the
    running TH/CH/events servers right away. The push sends ONLY /admins
    commands: running sessions are never interrupted.
    """
    try:
        with _cur() as cur:
            cur.execute(
                "SELECT a.server, a.steam_id, p.driver_name"
                "  FROM webadmin.server_admins a"
                "  LEFT JOIN mart.v_driver_profile p USING (steam_id)"
            )
            rows = cur.fetchall()
    except Exception:
        return
    for server in SERVER_UNIX_USER:
        admins = [[str(OWNER_STEAM_ID), "owner"]]
        for r in rows:
            if r["server"] == server and int(r["steam_id"]) != OWNER_STEAM_ID:
                admins.append([str(r["steam_id"]), r["driver_name"] or ""])
        cfg = _load_config(server)
        if cfg.get("ingame_admins") == admins:
            continue
        cfg["ingame_admins"] = admins
        try:
            _save_config(server, cfg)
        except OSError:
            continue
        if server in ("tripleheat", "casual_heat", "events"):
            _apply_ingame_admins_now(server, admins)


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
CONTENT_CACHE = "/srv/tsura/server_config/.content.{server}.json"


def _server_content(server: str) -> dict:
    """{'track': {name: guid}, 'vehicle': {name: guid}} from the server's files.

    This is what makes freshly uploaded content usable: the race database only
    knows what has already been driven, so before this a new track or car could
    neither be picked from the list nor saved. Reading the files themselves
    also supplies the GUID, which the TopDown panel needs and could otherwise
    only get from a race that has not happened yet.

    Cached per file by mtime+size, because a Levels folder holds ~1300 files.
    """
    kinds = {"track": ("track", ".lvl"), "vehicle": ("vehicle", ".veh")}
    out = {"track": {}, "vehicle": {}}
    cache_path = CONTENT_CACHE.format(server=server)
    cache = _load_json(cache_path) or {}
    new_cache, dirty = {}, False
    for kind, (upload_kind, ext) in kinds.items():
        try:
            entries = list(os.scandir(_upload_dir(server, upload_kind)))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or not entry.name.lower().endswith(ext):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            key = f"{stat.st_mtime_ns}:{stat.st_size}"
            cache_key = f"{kind}/{entry.name}"
            cached = cache.get(cache_key)
            if not cached or cached.get("key") != key:
                dirty = True
                try:
                    info = read_content_file(entry.path)
                    cached = {"key": key, "name": info["name"],
                              "guid": info["guid"]}
                except Exception:
                    # An unreadable file must not cost us the other 1300.
                    cached = {"key": key, "name": None, "guid": None}
            new_cache[cache_key] = cached
            if cached.get("name"):
                out[kind][cached["name"]] = cached.get("guid")
    if dirty or len(new_cache) != len(cache):
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(new_cache, fh)
            os.chmod(cache_path, 0o664)
        except OSError:
            pass          # a cache we cannot write is slow, not broken
    return out


def _installed_names(server: str, kind: str) -> set:
    """Names of the `kind` files installed on `server` (empty if unknown)."""
    if server not in UPLOAD_SERVERS:
        return set()
    return set(_server_content(server)[kind])


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
    known = set(_known_tracks(server) if kind == "track"
                else _known_vehicles(server))
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


# --------------------------------------------------------------- topdown
# The heat controller's parts bin and its AI lines. Both are world-readable
# under /home/topdown; the panel only ever reads them.
TOPDOWN_HOME = SERVER_HOME["topdown"]
TOPDOWN_PARTS_DIR = os.path.join(TOPDOWN_HOME, "session_parts")
TOPDOWN_CAMFILE = os.path.join(TOPDOWN_HOME, "td", "camfile.py")
TOPDOWN_AI_DIR = os.path.join(TOPDOWN_HOME, "server", "config", "AI")

# ai-<track guid>-<car guid>.aid — the layout the game exports.
AI_LINE_RE = re.compile(
    r"^ai-(?P<track>[0-9a-z]+-[0-9a-z]+)-(?P<car>[0-9a-z]+-[0-9a-z]+)\.aid$",
    re.IGNORECASE)

# ai.aiSkill is an enum, not a scale (read out of the game's metadata,
# 2026-08-11). 10-12 need a matching AI/customN.aic on the server: McVizn's
# export sat on 10 with no file, which is why the bots were odd for weeks.
AI_SKILLS = [
    (1, "Low"), (2, "Medium Low"), (3, "Medium"), (4, "Medium High"),
    (5, "High"), (10, "Custom 1 (needs AI/custom1.aic)"),
    (11, "Custom 2 (needs AI/custom2.aic)"),
    (12, "Custom 3 (needs AI/custom3.aic)"), (100, "Mixed"),
]

HUMAN_START_POSITIONS = [
    (0, "Normal grid slot"), (1, "Forced at the first event only"),
    (2, "Always forced to the back"),
]

# What the panel lets an admin change about a camera. Everything else in a
# .cam file (look mode, prediction, trackside timing) stays as exported.
CAMERA_FIELDS = [
    ("distance", "Zoom (distance)", "0.01"),
    ("verticalAngle", "Vertical angle", "0.01"),
    ("horizontalAngle", "Heading", "0.01"),
    ("fov", "Field of view", "0.01"),
]


def _topdown_camfile():
    """The controller's own .cam decoder, loaded from its file.

    Imported rather than copied so there is exactly one implementation of a
    format that took a while to work out. If it ever moves, the panel loses
    the camera read-out but stays usable.
    """
    import importlib.util
    cached = getattr(_topdown_camfile, "_mod", None)
    if cached is not None:
        return cached
    try:
        spec = importlib.util.spec_from_file_location(
            "topdown_camfile", TOPDOWN_CAMFILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        mod = None
    _topdown_camfile._mod = mod
    return mod


def _topdown_parts() -> dict:
    """Track name -> {guid, camera} for every track McVizn has exported."""
    out = {}
    camfile = _topdown_camfile()
    tracks_dir = os.path.join(TOPDOWN_PARTS_DIR, "tracks")
    try:
        names = sorted(os.listdir(tracks_dir))
    except OSError:
        return out
    for name in names:
        level = _load_json(os.path.join(tracks_dir, name, "level.json"))
        if not level or not level.get("name"):
            continue
        camera = None
        if camfile:
            try:
                camera = camfile.decode_file(
                    os.path.join(tracks_dir, name, "camera5.cam"))
            except Exception:
                camera = None
        out[level["name"]] = {"camera": camera}
    return out


def _topdown_ai_pairs() -> set:
    """(track guid, car guid) pairs that have an AI driving line on disk."""
    pairs = set()
    try:
        names = os.listdir(TOPDOWN_AI_DIR)
    except OSError:
        return pairs
    for fn in names:
        m = AI_LINE_RE.match(fn)
        if m:
            pairs.add((m.group("track").lower(), m.group("car").lower()))
    return pairs


def _guid_lookup(column: str, name_column: str) -> dict:
    """Name -> GUID for everything ever raced on a TSURA server.

    The result files carry both, so the panel can fill in the GUID of a track
    or car instead of asking an admin for a string they have no way to know.
    """
    try:
        with _cur() as cur:
            cur.execute(
                f"SELECT DISTINCT {name_column} AS name, {column} AS guid"
                f"  FROM mart.v_race_results"
                f" WHERE {column} IS NOT NULL AND {name_column} IS NOT NULL"
                f" ORDER BY 1"
            )
            return {r["name"]: r["guid"] for r in cur.fetchall()}
    except Exception:
        return {}


def _topdown_known_guids() -> tuple:
    """(tracks, cars) as name -> GUID, from the race DB plus installed files.

    The panel refuses to save a track or car without a GUID, so without the
    installed files freshly uploaded content stays unusable until it has been
    raced — which it cannot be until it is in the pool.
    """
    content = _server_content("topdown")
    tracks = _guid_lookup("track_guid", "track_name")
    cars = _guid_lookup("vehicle_guid", "vehicle_name")
    # The file is the better source: it is what this server actually loads.
    tracks.update({n: g for n, g in content["track"].items() if g})
    cars.update({n: g for n, g in content["vehicle"].items() if g})
    return tracks, cars


def _rows_from_form(prefix: str) -> list:
    """Collect indexed form rows (`t0_name`, `t1_name`, …) in order.

    Rows whose delete box is ticked, and the trailing blank "add" row, drop
    out here so the caller only sees what should be saved.
    """
    indexes = set()
    pattern = re.compile(rf"^{prefix}(\d+)_")
    for key in request.form:
        m = pattern.match(key)
        if m:
            indexes.add(int(m.group(1)))
    rows = []
    for i in sorted(indexes):
        if request.form.get(f"{prefix}{i}_delete") == "1":
            continue
        row = {k[len(f"{prefix}{i}_"):]: v.strip()
               for k, v in request.form.items()
               if k.startswith(f"{prefix}{i}_")}
        if not row.get("name"):
            continue
        rows.append(row)
    return rows


def _num_or_none(raw: str, label: str, lo: float, hi: float):
    if raw in (None, ""):
        return None
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(f"{label}: '{raw}' is not a number")
    if not lo <= val <= hi:
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}")
    return val


def _topdown_tracks_from_form(old_tracks: dict) -> list:
    """Build the track list out of the posted rows.

    Camera values are stored only where they differ from what the exported
    camera5.cam already says, so an untouched track keeps using McVizn's file
    byte for byte and a re-export still reaches the server.
    """
    parts = _topdown_parts()
    tracks = []
    for row in _rows_from_form("t"):
        name = row["name"]
        guid = row.get("guid", "").strip()
        if not guid and name not in parts:
            raise ValueError(
                f"'{name}': a track that has no session export needs its GUID "
                f"— pick the track from the list to fill it in automatically.")
        track = dict(old_tracks.get(name) or {})
        track["name"] = name
        if guid:
            track["guid"] = guid
        track["laps"] = int(_num_or_none(row.get("laps"), f"'{name}' laps",
                                         1, 200) or 1)
        track["weight"] = 1.0 if row.get("enabled") == "1" else 0.0
        variance = _num_or_none(row.get("variance"), f"'{name}' lap variance",
                                0, 100)
        if variance is None:
            track.pop("lap_bonus_pct", None)
        else:
            track["lap_bonus_pct"] = variance
        if row.get("camera_from"):
            track["camera_from"] = row["camera_from"]
        else:
            track.pop("camera_from", None)

        # Compare against whatever the track would use without an override, so
        # an unedited form stores nothing at all.
        exported = (parts.get(name) or {}).get("camera") or {}
        if not exported and row.get("camera_from"):
            exported = (parts.get(row["camera_from"]) or {}).get("camera") or {}
        camera = {}
        for key, label, _step in CAMERA_FIELDS:
            value = _num_or_none(row.get(f"cam_{key}"), f"'{name}' {label}",
                                 -100000, 100000)
            if value is None:
                continue
            # Only a real difference is worth overriding the export with.
            if key in exported and abs(float(exported[key]) - value) < 1e-4:
                continue
            camera[key] = value
        if row.get("camera_reset") == "1" or not camera:
            track.pop("camera_settings", None)
        else:
            track["camera_settings"] = camera
        tracks.append(track)
    if not tracks:
        raise ValueError("At least one track is needed — a heat without "
                         "tracks cannot start.")
    if not any(t["weight"] > 0 for t in tracks):
        raise ValueError("At least one track must stay enabled.")
    _reject_duplicates([t["name"] for t in tracks], "track")
    return tracks


def _reject_duplicates(names, what: str) -> None:
    """Two rows with the same name make the per-name settings ambiguous."""
    seen, dupes = set(), []
    for name in names:
        key = name.lower()
        if key in seen and name not in dupes:
            dupes.append(name)
        seen.add(key)
    if dupes:
        raise ValueError(
            f"The same {what} is listed twice: {', '.join(dupes)}. "
            f"Remove one of the rows.")


def _topdown_vehicles_from_form(cfg: dict) -> tuple:
    """The car pool plus the per-car drafting and bot-strength blocks.

    Both the tow and the bots' pace belong to the car rather than to the heat,
    so each is stored only where it differs from the shared value.
    """
    vehicles, drafting_by_vehicle, ai_by_vehicle = [], {}, {}
    base = cfg.get("drafting") or {}
    shared_skill = (cfg.get("ai") or {}).get("aiSkill")
    for row in _rows_from_form("v"):
        name = row["name"]
        guid = row.get("guid", "").strip()
        if not guid:
            raise ValueError(
                f"'{name}': a car needs its GUID — pick the car from the list "
                f"to fill it in automatically.")
        vehicles.append({"name": name, "guid": guid,
                         "weight": 1.0 if row.get("enabled") == "1" else 0.0})
        overrides = {}
        for key, label in (("maxDraftingDistance", "draft distance"),
                           ("draftingSpeedEffect", "draft speed effect")):
            value = _num_or_none(row.get(key), f"'{name}' {label}", 0, 1000)
            if value is None or _approx_equal(value, base.get(key)):
                continue
            overrides[key] = value
        if overrides:
            drafting_by_vehicle[name] = overrides
        skill = row.get("ai_skill", "").strip()
        if skill and int(skill) != shared_skill:
            ai_by_vehicle[name] = {"aiSkill": int(skill)}
    if not vehicles:
        raise ValueError("At least one car is needed.")
    if not any(v["weight"] > 0 for v in vehicles):
        raise ValueError("At least one car must stay enabled.")
    _reject_duplicates([v["name"] for v in vehicles], "car")
    return vehicles, drafting_by_vehicle, ai_by_vehicle


@admin_bp.route("/topdown", methods=["GET", "POST"])
@_server_admin_required("topdown")
def topdown():
    if request.method == "POST":
        if not _csrf_ok():
            abort(400)
        cfg = _load_config("topdown")
        try:
            old_tracks = {t.get("name"): t for t in cfg.get("tracks") or []}
            names = [r["name"] for r in _rows_from_form("t")]
            _check_content_names("track", names, "topdown",
                                 extra_known=list(old_tracks))
            car_names = [r["name"] for r in _rows_from_form("v")]
            _check_content_names("vehicle", car_names, "topdown",
                                 extra_known=[v.get("name") for v in
                                              cfg.get("vehicles") or []])

            cfg["tracks_per_heat"] = _form_int(
                "tracks_per_heat", "Tracks per heat", 1, 20)
            cfg["lap_bonus_max_pct"] = _form_int(
                "lap_bonus_max_pct", "Default lap variance", 0, 100)
            # 0 = off, and then every track's own lap count applies again.
            cfg["laps_override"] = _form_int(
                "laps_override", "Test-phase lap count", 0, 200)
            cfg["countdown_seconds"] = _form_int(
                "countdown_seconds", "Countdown", 5, 600)
            cfg["cooldown_seconds"] = _form_int(
                "cooldown_seconds", "Cooldown between heats", 5, 600)
            cfg["quali"] = dict(cfg.get("quali") or {},
                                laps=_form_int("quali_laps", "Qualifying laps",
                                               1, 10))

            drafting = dict(cfg.get("drafting") or {})
            drafting["maxDraftingDistance"] = _form_int(
                "draft_distance", "Draft distance", 0, 1000)
            drafting["draftingSpeedEffect"] = _form_int(
                "draft_speed", "Draft speed effect", 0, 100)
            cfg["drafting"] = drafting

            cfg["bot_fill"] = _form_int("bot_fill", "Grid size with bots", 0, 20)
            cfg["max_drivers"] = _form_int("max_drivers", "Maximum drivers", 1, 20)
            cfg["bots_off_from_humans"] = _form_int(
                "bots_off_from_humans", "Bots off from … humans", 1, 20)
            ai = dict(cfg.get("ai") or {})
            ai["aiSkill"] = _form_int("ai_skill", "Bot strength", 1, 100)
            ai["humanStartPosition"] = _form_int(
                "human_start", "Human start position", 0, 2)
            cfg["ai"] = ai
            _store_collision(cfg)

            # Tracks and cars last: they raise the most specific errors, and a
            # rejected save must leave the whole config untouched.
            cfg["tracks"] = _topdown_tracks_from_form(old_tracks)
            vehicles, by_vehicle, ai_by_vehicle = _topdown_vehicles_from_form(cfg)
            cfg["vehicles"] = vehicles
            cfg["drafting_by_vehicle"] = by_vehicle
            cfg["ai_by_vehicle"] = ai_by_vehicle
            # Superseded by the pool; leaving them would be a second truth.
            cfg.pop("vehicle", None)
            cfg.pop("vehicle_guid", None)

            _save_config("topdown", cfg)
            flash("TopDown config saved — it applies to the next heat "
                  "(no restart needed).", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except OSError as exc:
            flash(f"Could not write config: {exc}", "danger")
        return redirect(url_for("admin.topdown"))

    cfg = _load_config("topdown")
    parts = _topdown_parts()
    ai_pairs = _topdown_ai_pairs()
    track_guids, car_guids = _topdown_known_guids()
    vehicles = cfg.get("vehicles") or []
    if not vehicles and cfg.get("vehicle"):
        vehicles = [{"name": cfg["vehicle"], "guid": cfg.get("vehicle_guid", ""),
                     "weight": 1.0}]
    by_vehicle = cfg.get("drafting_by_vehicle") or {}
    ai_by_vehicle = cfg.get("ai_by_vehicle") or {}

    tracks = []
    for t in cfg.get("tracks") or []:
        exported = (parts.get(t.get("name")) or {}).get("camera")
        overrides = t.get("camera_settings") or {}
        # Show what the track will actually be driven with: the exported
        # camera, or the one it borrows, with the config's edits on top.
        borrowed = (parts.get(t.get("camera_from")) or {}).get("camera")
        camera = dict(exported or borrowed or {})
        camera.update(overrides)
        if exported is not None:
            source = "config" if overrides else "session export"
        elif t.get("camera_from"):
            source = f"copied from {t['camera_from']}"
        else:
            source = "config" if overrides else "default camera"
        tracks.append({
            **t,
            "enabled": float(t.get("weight", 1) or 0) > 0,
            "camera": camera,
            "camera_source": source,
            "has_export": t.get("name") in parts,
            "bots": {v.get("name"): (str(t.get("guid", "")).lower(),
                                     str(v.get("guid", "")).lower()) in ai_pairs
                     for v in vehicles},
        })

    return render_template(
        "admin/topdown.html",
        meta=SERVERS["topdown"],
        cfg=cfg,
        tracks=tracks,
        vehicles=[{**v,
                   "enabled": float(v.get("weight", 1) or 0) > 0,
                   "drafting": by_vehicle.get(v.get("name")) or {},
                   "ai_skill": (ai_by_vehicle.get(v.get("name")) or {}).get(
                       "aiSkill")}
                  for v in vehicles],
        drafting=cfg.get("drafting") or {},
        ai=cfg.get("ai") or {},
        ai_skills=AI_SKILLS,
        human_start_positions=HUMAN_START_POSITIONS,
        camera_fields=CAMERA_FIELDS,
        parts_tracks=sorted(parts),
        track_guids=track_guids,
        car_guids=car_guids,
        track_options=sorted(track_guids),
        vehicle_options=sorted(car_guids),
        custom_aic_missing=_topdown_missing_aic(cfg),
        **_collision_context("topdown", cfg),
        **_upload_context("topdown"),
        **_control_context("topdown"),
    )


def _topdown_missing_aic(cfg: dict):
    """Warn when bots are set to a Custom group the server has no file for."""
    skill = (cfg.get("ai") or {}).get("aiSkill")
    if skill not in (10, 11, 12):
        return None
    name = f"custom{skill - 9}.aic"
    if os.path.exists(os.path.join(TOPDOWN_AI_DIR, name)):
        return None
    return name


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
        "topdown": url_for("admin.topdown"),
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
            # top section = only the values that get randomized per race;
            # everything else lives in the advanced params (diff-based)
            race = {}
            if server == "tripleheat":
                race["laps_min"], race["laps_max"] = _form_range("laps", "Race laps", 1, 1000)
            else:
                race["max_compounds"] = _form_int("max_compounds", "Max tire compounds", 1, 2)
            race["fuel_min"], race["fuel_max"] = _form_range("fuel", "Fuel (full-gas time)", 1, 100000)
            race["tires_min"], race["tires_max"] = _form_range("tires", "Tire endurance", 1, 100000)
            base = {p: d for _sec, fields in _event_param_specs(server)
                    for p, d, _disp in fields}
            cfg["quali"] = {"params": _parse_param_overrides(
                "qp__", _column_defaults(server, base, "quali"), "Quali")}
            race["params"] = _parse_param_overrides(
                "rp__", _column_defaults(server, base, "race"), "Race")
            cfg["race"] = race
            _store_collision(cfg)
            _save_config(server, cfg)
            flash(f"{meta['label']} config saved — used at the next session start.",
                  "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except OSError as exc:
            flash(f"Could not write config: {exc}", "danger")
        return redirect(url_for(endpoint))

    cfg = _load_config(server)
    specs = _event_param_specs(server)
    base = {p: d for _sec, fields in specs for p, d, _disp in fields}
    qdisp = {p: _fmt_default(v)
             for p, v in _column_defaults(server, base, "quali").items()}
    rdisp = {p: _fmt_default(v)
             for p, v in _column_defaults(server, base, "race").items()}
    return render_template(
        "admin/heat_config.html",
        server=server,
        meta=meta,
        endpoint=endpoint,
        cfg=cfg,
        tracks_text=_fmt_weighted(cfg.get("tracks", [])),
        cars_text=_fmt_weighted(cfg.get("cars", [])) if server == "casual_heat" else "",
        vehicles_text="\n".join(cfg.get("vehicles", [])) if server == "tripleheat" else "",
        is_owner=owner,
        track_options=_known_tracks(server),
        vehicle_options=_known_vehicles(server),
        param_specs=specs,
        qdisp=qdisp,
        rdisp=rdisp,
        randomized_paths=RANDOMIZED_PATHS.get(server, set()),
        quali_params=cfg.get("quali", {}).get("params", {}),
        race_params=cfg.get("race", {}).get("params", {}),
        **_collision_context(server, cfg),
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
            _store_collision(cfg)
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
        track_options=_known_tracks("hotlapping"),
        vehicle_options=_known_vehicles("hotlapping"),
        **_collision_context("hotlapping", cfg),
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
    ("adjust_collision", "/physics.adjustVehicleCollisionSettings", "bool"),
    ("collisionSteadiness", "/physics.collisionSteadiness", "num"),
    ("collisionExtraStability", "/physics.collisionExtraStability", "num"),
    ("stuckAvoidance", "/physics.stuckAvoidance", "num"),
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


@admin_bp.route("/<server>/apply", methods=["POST"])
def apply_session(server):
    """'Apply now': request a fresh session with the saved config.

    A per-server cron (apply_web_session.py, runs as the game user)
    consumes the request within a minute: it restarts the server first if
    content files are newer than the running process, then triggers the
    normal session-start flow (incl. /refreshfiles at session init).
    """
    if server not in ("tripleheat", "casual_heat"):
        abort(404)
    if not is_server_admin(g.get("current_steam_id"), server):
        abort(403)
    if not _csrf_ok():
        abort(400)
    req = os.path.join(CONFIG_DIR, f"{server}.apply_session")
    try:
        with open(req, "w", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} by {g.current_steam_id}\n")
        os.chmod(req, 0o664)
    except OSError as exc:
        flash(f"Could not request apply: {exc}", "danger")
        return redirect(url_for(PANEL_ENDPOINT[server]))
    flash("Apply requested — the server picks it up within a minute, restarts "
          "only if freshly uploaded files need scanning, and then starts a "
          "fresh session with the saved config (ready in ~1–3 minutes).",
          "success")
    return redirect(url_for(PANEL_ENDPOINT[server]))


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
    if kind not in SERVER_UPLOAD_KINDS.get(server, ()):
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
            if kind == "ai_line" and not AI_LINE_RE.match(name):
                # The game finds a driving line purely by file name; a renamed
                # file is silently ignored and the race just runs without bots.
                raise ValueError(
                    f"{name}: an AI line must keep its exported name "
                    f"(ai-<track guid>-<car guid>.aid)")
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
        if kind == "ai_line":
            note = ("— the heat controller sees them from the next heat; "
                    "restart the server if a race still runs without bots")
        else:
            note = ("— the running server scans files only at startup: loaded "
                    "after the next restart (daily ~5:00, plus 20:45 before "
                    "sessions) or hit “Restart server” to use them right away")
        flash(
            f"Uploaded {len(saved)} {spec['label']} file(s): "
            f"{', '.join(saved)} {note}.",
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
    _sync_ingame_admins()
    flash(f"{name or steam_id} is now a {SERVERS[server]['label']} admin "
          "(panel access + in-game admin).", "success")
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
    _sync_ingame_admins()
    flash("Admin removed (panel access + in-game).", "success")
    return redirect(url_for("admin.admins"))


# ------------------------------------------------------------ track flags
_COUNTRY_CODE_RE = re.compile(r"^[a-z]{2}(-[a-z]{2,3})?$")


@admin_bp.route("/tracks", methods=["GET", "POST"])
def tracks():
    """Assign a country flag to every track (open to all server admins).

    The website shows a flag next to each track name; unknown/fictional
    tracks use the neutral 'xx' placeholder. New tracks appear here
    automatically once the pipeline has ingested a race on them.
    """
    sid = g.get("current_steam_id")
    if not user_admin_servers(sid):
        abort(403)

    if request.method == "POST":
        if not _csrf_ok():
            abort(400)
        name = (request.form.get("track_name") or "").strip()
        if request.form.get("set_global"):
            code = "un"          # fictional/global track — counts as assigned
        else:
            code = (request.form.get("country_code") or "").strip().lower()
        if not name or not _COUNTRY_CODE_RE.fullmatch(code):
            flash("Invalid country code — use a lowercase flag-icons code "
                  "like 'de' or 'us', the Global button for fictional "
                  "tracks, or 'xx' (unknown).", "danger")
        else:
            with _cur() as cur:
                cur.execute(
                    "INSERT INTO webadmin.track_countries"
                    " (track_name, country_code, updated_by)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (track_name) DO UPDATE"
                    " SET country_code = EXCLUDED.country_code,"
                    "     updated_by = EXCLUDED.updated_by,"
                    "     updated_at = now()",
                    (name, code, sid),
                )
                cur.connection.commit()
            flash(f"Saved: {name} → {code}", "success")
        return redirect(url_for("admin.tracks"))

    with _cur() as cur:
        cur.execute(
            "SELECT t.name AS track_name, c.country_code"
            "  FROM (SELECT DISTINCT name FROM base.tracks) t"
            "  LEFT JOIN webadmin.track_countries c ON c.track_name = t.name"
            " ORDER BY (COALESCE(c.country_code, 'xx') <> 'xx'), t.name")
        rows = cur.fetchall()
    unassigned = sum(1 for r in rows
                     if (r["country_code"] or "xx") == "xx")
    return render_template("admin/tracks.html", rows=rows,
                           unassigned=unassigned,
                           csrf_token=make_csrf_token(
                               g.session_id, current_app.config["SECRET_KEY"]))
