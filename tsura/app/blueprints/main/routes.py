"""Routes for the main (public) blueprint."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict, OrderedDict
from typing import List

import psycopg
import requests
from flask import abort, current_app, g, redirect, render_template, request, url_for

from . import main_bp
from ...extensions import db_pool, make_csrf_token


# ── Input validation patterns for profile edit ───────────────────────────────
_TEAM_TAG_RE    = re.compile(r"^[A-Za-z0-9]{1,3}$")
_WORKSHOP_RE    = re.compile(
    r"^https://steamcommunity\.com/(sharedfiles|workshop)/filedetails/\?id=\d+$"
)
_TWITCH_RE      = re.compile(
    r"^https://(www\.)?twitch\.tv/[A-Za-z0-9_]{1,25}/?$"
)
_YOUTUBE_RE     = re.compile(
    r"^https://(www\.)?youtube\.com/(@[A-Za-z0-9_.\-]{1,100}|channel/[A-Za-z0-9_\-]{1,64})/?$"
)


def _csrf_ok() -> bool:
    submitted = request.form.get("csrf_token", "")
    sid = g.get("session_id")
    if not sid:
        return False
    expected = make_csrf_token(sid, current_app.config["SECRET_KEY"])
    return hmac.compare_digest(submitted, expected)


# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #
def _fmt_lap_time(seconds: float) -> str:
    if seconds is None:
        return "-:--.----"
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes}:{rest:07.4f}"  # M:SS.ffff


def _fmt_race_time(seconds: float) -> str:
    if seconds is None:
        return "—"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"  # MM:SS.fff


def _fmt_time_diff(seconds: float) -> str:
    """Format a positive time gap as '+MM:SS.FFF' (FFF = milliseconds)."""
    if seconds is None or seconds < 0:
        return "—"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"+{m:02d}:{s:06.3f}"


# Country name (as stored in DB) → ISO-3166-1 alpha-2 lowercase code
# Used with the flag-icons CSS library: <span class="fi fi-{code}"></span>
_FLAG_CODE_MAP = {
    "Finland": "fi", "Germany": "de", "United_Kingdom": "gb",
    "Netherlands": "nl", "Italy": "it", "United_States": "us",
    "Belgium": "be", "Russia": "ru", "Brazil": "br", "Spain": "es",
    "Austria": "at", "France": "fr", "Switzerland": "ch", "Turkey": "tr",
    "Czech_Republic": "cz", "Argentina": "ar", "Hungary": "hu",
    "Canada": "ca", "Portugal": "pt", "Poland": "pl", "Greece": "gr",
    "Guernsey": "gg", "India": "in", "Australia": "au", "Ukraine": "ua",
    "Norway": "no", "Serbia": "rs", "Denmark": "dk",
    "Bosnia_and_Herzegovina": "ba", "Malaysia": "my", "Ireland": "ie",
    "Isle_of_Man": "im", "Brunei": "bn", "Cyprus": "cy", "Kuwait": "kw",
    "Romania": "ro", "Afghanistan": "af", "Morocco": "ma", "Chile": "cl",
    "Sweden": "se", "Estonia": "ee", "Monaco": "mc", "Thailand": "th",
}


def _flag_code(flag_text: str | None) -> str:
    """Return ISO-3166-1 alpha-2 code for use with flag-icons CSS, or ''."""
    if not flag_text:
        return ""
    return _FLAG_CODE_MAP.get(flag_text, "")


# ── Track → country flags (webadmin.track_countries, admin-maintained) ──────
_TRACK_FLAGS: dict = {"data": {}, "loaded_at": 0.0}


def _track_flag_map() -> dict:
    """track_name -> flag-icons code, cached for 5 minutes."""
    now = time.monotonic()
    if now - _TRACK_FLAGS["loaded_at"] > 300 or not _TRACK_FLAGS["data"]:
        try:
            conn = db_pool.get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('webadmin.track_countries') "
                            "IS NOT NULL")
                if cur.fetchone()[0]:
                    cur.execute("SELECT track_name, country_code "
                                "FROM webadmin.track_countries")
                    _TRACK_FLAGS["data"] = dict(cur.fetchall())
        except Exception:
            conn = g.pop("db_conn", None)
            if conn is not None:
                conn.rollback()
                g.db_conn = conn
        _TRACK_FLAGS["loaded_at"] = now
    return _TRACK_FLAGS["data"]


@main_bp.app_context_processor
def _inject_track_flag():
    def track_flag(name: str | None) -> str:
        """flag-icons code for a track name; 'xx' (globe placeholder) if unknown."""
        return _track_flag_map().get(name or "") or "xx"
    return {"track_flag": track_flag}


API_URL = (
    "https://api.steampowered.com/IGameServersService/GetServerList/v1/"
    f"?key={os.environ.get('TSURA_STEAM_API_KEY','')}&filter=appid%5C1478340"
)

_RACE_SERVERS = ("events", "tripleheat", "casual_heat")


# Servers selectable on the /races page (key = DB server name).
RACE_SERVERS = {
    "events": "Event Server",
    "tripleheat": "Triple Heat",
    "casual_heat": "Casual Heat",
    "career": "Career",
}


def _last_day_summary(cur, server: str) -> list:
    """All races on the most recent race day for a given server, max 5."""
    cur.execute(
        """
        WITH last_date AS (
            SELECT DATE(MAX(utc_start_time) AT TIME ZONE 'Europe/Berlin') AS d
            FROM mart.v_race_results
            WHERE server = %(server)s
        )
        SELECT DISTINCT
            r.session_id,
            r.utc_start_time,
            r.track_name,
            MIN(r.human_participant_count) AS human_count,
            MIN(r.driver_name) FILTER (WHERE r.position = 1) AS winner
        FROM mart.v_race_results r, last_date
        WHERE r.server = %(server)s
          AND DATE(r.utc_start_time AT TIME ZONE 'Europe/Berlin') = last_date.d
        GROUP BY r.session_id, r.utc_start_time, r.track_name
        ORDER BY r.utc_start_time ASC
        LIMIT 5;
        """,
        {"server": server},
    )
    return cur.fetchall()


# --------------------------------------------------------------------------- #
#  INDEX                                                                      #
# --------------------------------------------------------------------------- #
# live server name (Steam master) -> admin-area server key
SERVER_NAME_KEYS = {
    "#1 Event Server": "events",
    "#2 Hotlapping": "hotlapping",
    "#3 TripleHeat": "tripleheat",
    "Casual Wed Heat": "casual_heat",
    "TSURA Career": "career",
}


def _server_admin_names(cur):
    """server key -> [{steam_id, name}] from the web admin lists."""
    out = {}
    try:
        cur.execute(
            "SELECT a.server, a.steam_id, p.driver_name"
            "  FROM webadmin.server_admins a"
            "  LEFT JOIN mart.v_driver_profile p USING (steam_id)"
            " ORDER BY p.driver_name NULLS LAST, a.steam_id"
        )
        for r in cur.fetchall():
            out.setdefault(r["server"], []).append(
                {"steam_id": r["steam_id"], "name": r["driver_name"]})
    except Exception:
        pass
    return out


@main_bp.route("/")
def index():
    """Landing page: per-server last-day summaries, hotlap combo, server status."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT group_id,
                   track_name,
                   session_start,
                   session_end,
                   cars_used,
                   driver_count,
                   total_laps,
                   best_lap_time
              FROM mart.v_hotlap_grouped_sessions
          ORDER BY session_end DESC
             LIMIT 1;
            """
        )
        hotlap = cur.fetchone()

        hotlap_top = []
        if hotlap:
            cur.execute(
                """
                SELECT steam_id, driver_name, driver_flag, vehicle_name,
                       lap_time,
                       lap_time - MIN(lap_time) OVER () AS diff_to_best
                  FROM mart.v_hotlap_group_results
                 WHERE group_id = %s AND is_best_lap = true
              ORDER BY lap_time
                 LIMIT 3;
                """,
                (hotlap["group_id"],),
            )
            hotlap_top = [{**r,
                           "flag_code": _flag_code(r["driver_flag"]),
                           "lap_fmt": _fmt_lap_time(r["lap_time"]),
                           "diff_fmt": _fmt_time_diff(r["diff_to_best"])}
                          for r in cur.fetchall()]

        admin_names = _server_admin_names(cur)

        summary_events = _last_day_summary(cur, "events")
        summary_heats  = _last_day_summary(cur, "tripleheat")
        summary_casual = _last_day_summary(cur, "casual_heat")
        summary_career = _last_day_summary(cur, "career")

        # ELO top list (same source as /elo_heats)
        cur.execute(
            """
            SELECT steam_id, driver_name, driver_flag, display_tag, heat_elo
              FROM mart.v_driver_profile
             WHERE heat_elo IS NOT NULL AND heat_total_races >= 3
          ORDER BY heat_elo DESC
             LIMIT 10;
            """
        )
        elo_top = [{**r, "flag_code": _flag_code(r["driver_flag"])}
                   for r in cur.fetchall()]

    # server list -----------------------------------------------------------
    servers: list[dict] = []
    try:
        resp = requests.get(API_URL, timeout=3)
        for s in resp.json().get("response", {}).get("servers", []):
            servers.append(
                {
                    "name": s.get("name", "N/A"),
                    "players": s.get("players", 0),
                    "max_players": s.get("max_players", 0),
                    "secure": s.get("secure", False),
                }
            )
    except Exception:
        pass

    for srv in servers:
        # Steam reports "serverName/currentEventName" — match on the prefix
        base_name = srv["name"].split("/")[0].strip()
        srv["admins"] = admin_names.get(SERVER_NAME_KEYS.get(base_name, ""), [])

    servers.sort(key=lambda x: (-x["players"], x["name"].lower()))

    return render_template(
        "index.html",
        servers=servers,
        hotlap=hotlap,
        hotlap_top=hotlap_top,
        elo_top=elo_top,
        summary_events=summary_events,
        summary_heats=summary_heats,
        summary_casual=summary_casual,
        summary_career=summary_career,
    )


# --------------------------------------------------------------------------- #
#  HOTLAPPING LIST                                                            #
# --------------------------------------------------------------------------- #
@main_bp.route("/hotlapping")
def hotlapping():
    """Hotlap sessions grouped by consecutive same-track runs."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT group_id,
                   track_name,
                   session_start,
                   session_end,
                   event_count,
                   driver_count,
                   total_laps,
                   cars_used,
                   best_lap_time
              FROM mart.v_hotlap_grouped_sessions
          ORDER BY session_end DESC;
            """
        )
        sessions = cur.fetchall()

    return render_template("hotlapping.html", sessions=sessions)


# --------------------------------------------------------------------------- #
#  HOTLAPPING DETAIL                                                          #
# --------------------------------------------------------------------------- #
@main_bp.route("/hotlapping/<group_id>")
def hotlapping_detail(group_id: str):
    """Best lap per driver and top-500 laps across all events of a grouped session."""
    conn = db_pool.get_conn()

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT
                steam_id,
                driver_name,
                vehicle_name,
                track_name,
                utc_start_time,
                lap_time,
                sector_times,
                lap_time - MIN(lap_time) OVER () AS diff_to_best
              FROM mart.v_hotlap_group_results
             WHERE group_id = %s
               AND is_best_lap = true
          ORDER BY lap_time;
            """,
            (group_id,),
        )
        best_rows = cur.fetchall()

        cur.execute(
            """
            SELECT
                steam_id,
                driver_name,
                vehicle_name,
                utc_start_time,
                lap_time,
                sector_times
              FROM mart.v_hotlap_group_results
             WHERE group_id = %s
               AND lap_time IS NOT NULL
          ORDER BY lap_time
             LIMIT 500;
            """,
            (group_id,),
        )
        lap_rows = cur.fetchall()

    if not best_rows or not lap_rows:
        return render_template(
            "hotlapping_detail.html",
            group_id=group_id,
            track_name="Unknown",
            best=[], laps=[], n_sectors=0,
            best_sector_fmt=[], best_sector_driver=[],
            opt_lap="-:--.----", opt_diff="+0.0000",
            fastest_lap="-:--.----",
        )

    track_name = best_rows[0]["track_name"]

    # consistency per driver: ≥5 laps within 1%/0.3% of that driver's best ---
    driver_lap_times: dict[int, list[float]] = defaultdict(list)
    for r in lap_rows:
        if r["lap_time"] is not None:
            driver_lap_times[r["steam_id"]].append(float(r["lap_time"]))

    driver_consistent: dict[int, bool] = {}
    driver_very_consistent: dict[int, bool] = {}
    for sid, times in driver_lap_times.items():
        best_t = min(times)
        driver_consistent[sid] = sum(1 for t in times if t <= best_t * 1.01) >= 5
        driver_very_consistent[sid] = sum(1 for t in times if t <= best_t * 1.003) >= 5

    # sector bests & theoretical optimal lap --------------------------------
    n_sectors: int = len(best_rows[0]["sector_times"] or [])
    best_sector_vals: List[float] = [float("inf")] * n_sectors
    best_sector_drivers: List[str] = [""] * n_sectors

    def check_sectors(row):
        for idx, sec_time in enumerate((row["sector_times"] or [])[:n_sectors]):
            try:
                t = float(sec_time)
                if t < best_sector_vals[idx]:
                    best_sector_vals[idx] = t
                    best_sector_drivers[idx] = row["driver_name"]
            except Exception:
                continue

    for r in best_rows:
        check_sectors(r)
    for r in lap_rows:
        check_sectors(r)

    finite_sector_vals = [v for v in best_sector_vals if v != float("inf")]
    optimal_lap_sec = sum(finite_sector_vals) if finite_sector_vals else 0.0
    best_real_sec = float(lap_rows[0]["lap_time"])
    optimal_diff = (optimal_lap_sec - best_real_sec) if optimal_lap_sec > 0 else 0.0

    best_sector_fmt = [
        _fmt_lap_time(t) if t != float("inf") else "-:--.----"
        for t in best_sector_vals
    ]
    fastest_lap_fmt = _fmt_lap_time(best_rows[0]["lap_time"])

    best = [
        {
            "driver_id": row["steam_id"],
            "driver": row["driver_name"],
            "car": row["vehicle_name"],
            "lap": _fmt_lap_time(row["lap_time"]),
            "diff": (
                "-"
                if (row["diff_to_best"] or 0.0) == 0.0
                else f"+{row['diff_to_best']:.4f}"
            ),
            "consistent": driver_consistent.get(row["steam_id"], False),
            "very_consistent": driver_very_consistent.get(row["steam_id"], False),
            "sectors": [_fmt_lap_time(t) for t in (row["sector_times"] or [])],
            "ts": row["utc_start_time"],
        }
        for row in best_rows
    ]

    laps = [
        {
            "driver_id": row["steam_id"],
            "driver": row["driver_name"],
            "car": row["vehicle_name"],
            "lap": _fmt_lap_time(row["lap_time"]),
            "sectors": [_fmt_lap_time(t) for t in (row["sector_times"] or [])],
            "ts": row["utc_start_time"],
        }
        for row in lap_rows
    ]

    return render_template(
        "hotlapping_detail.html",
        group_id=group_id,
        track_name=track_name,
        best=best,
        laps=laps,
        n_sectors=n_sectors,
        best_sector_fmt=best_sector_fmt,
        best_sector_driver=best_sector_drivers,
        opt_lap=_fmt_lap_time(optimal_lap_sec),
        opt_diff=f"{optimal_diff:+.4f}",
        fastest_lap=fastest_lap_fmt,
    )


# --------------------------------------------------------------------------- #
#  ELO HEATS                                                                  #
# --------------------------------------------------------------------------- #
@main_bp.route("/elo-heats")
def elo_heats():
    """Tripleheat ELO leaderboard (drivers with >= 3 races)."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT steam_id,
                   driver_name,
                   driver_flag,
                   display_tag,
                   heat_elo,
                   heat_elo_delta,
                   heat_elo_trend_6,
                   heat_total_races,
                   heat_last_race_at
              FROM mart.v_driver_profile
             WHERE heat_elo IS NOT NULL
               AND heat_total_races >= 3
          ORDER BY heat_elo DESC;
            """
        )
        raw = cur.fetchall()

    records = [
        {**r, "flag_code": _flag_code(r["driver_flag"])}
        for r in raw
    ]

    return render_template("elo_heats.html", records=records)


# --------------------------------------------------------------------------- #
#  RACES LIST                                                                 #
# --------------------------------------------------------------------------- #
@main_bp.route("/races")
def races():
    """Race results, newest first, optionally filtered by ?server=.

    Events/heat servers only list races with ≥4 humans; the career
    server has no minimum (small championship grids are the norm there).
    """
    server = request.args.get("server")
    if server not in RACE_SERVERS:
        server = None
    servers = [server] if server else list(RACE_SERVERS)
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT
                session_id,
                utc_start_time,
                server,
                track_name,
                MIN(human_participant_count) AS human_count,
                STRING_AGG(DISTINCT vehicle_name, ', ' ORDER BY vehicle_name) AS cars,
                MIN(driver_name) FILTER (WHERE position = 1) AS winner
              FROM mart.v_race_results
             WHERE server = ANY(%(servers)s)
          GROUP BY session_id, utc_start_time, server, track_name
            HAVING MIN(human_participant_count) >= 4 OR server = 'career'
          ORDER BY utc_start_time DESC
             LIMIT 200;
            """,
            {"servers": servers},
        )
        race_list = cur.fetchall()

    return render_template("races.html", races=race_list,
                           server_filter=server, server_labels=RACE_SERVERS)


# --------------------------------------------------------------------------- #
#  RACE DETAIL                                                                #
# --------------------------------------------------------------------------- #
@main_bp.route("/races/<session_id>")
def race_detail(session_id: str):
    """Full result table for a single race session."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT
                session_id,
                utc_start_time,
                server,
                track_name,
                track_type,
                human_participant_count,
                finished_state,
                steam_id,
                driver_name,
                driver_flag,
                driver_clan,
                display_tag,
                vehicle_name,
                position,
                start_position,
                finish_time,
                laps_completed,
                fastest_lap,
                elo_value,
                elo_delta,
                current_elo
              FROM mart.v_race_results
             WHERE session_id = %s
          ORDER BY position ASC NULLS LAST;
            """,
            (session_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return render_template("race_detail.html", session_id=session_id,
                               meta=None, results=[],
                               stint_drivers=[], max_race_laps=0)

    meta = {
        "session_id":        rows[0]["session_id"],
        "utc_start_time":    rows[0]["utc_start_time"],
        "server":            rows[0]["server"],
        "track_name":        rows[0]["track_name"],
        "track_type":        rows[0]["track_type"],
        "participant_count": rows[0]["human_participant_count"],
        "finished_state":    rows[0]["finished_state"],
    }

    # find winner's time + laps for relative time calculation
    winner_time = None
    winner_laps = None
    for r in rows:
        if r["position"] == 1:
            winner_time = r["finish_time"]
            winner_laps = r["laps_completed"]
            break

    # Overall fastest lap in the session (for purple highlight)
    overall_fastest = min(
        (r["fastest_lap"] for r in rows if r.get("fastest_lap") is not None),
        default=None,
    )

    results = []
    for row in rows:
        pos = row["position"]
        ft = row["finish_time"]
        laps = row["laps_completed"]

        if pos == 1:
            time_display = _fmt_race_time(ft)
        elif laps is not None and winner_laps is not None and laps < winner_laps:
            lap_diff = winner_laps - laps
            time_display = f"+{lap_diff} lap{'s' if lap_diff != 1 else ''}"
        elif ft is not None and winner_time is not None:
            time_display = _fmt_time_diff(ft - winner_time)
        else:
            time_display = _fmt_race_time(ft)

        fl = row.get("fastest_lap")
        start_pos = row.get("start_position")
        pos_diff = (start_pos - pos) if (start_pos is not None and pos is not None) else None
        results.append(
            {
                "position":           pos,
                "start_position":     start_pos,
                "pos_diff":           pos_diff,
                "driver_id":          row["steam_id"],
                "driver_name":        row["driver_name"],
                "driver_flag":        _flag_code(row["driver_flag"]),
                "display_tag":        row["display_tag"],
                "vehicle":            row["vehicle_name"],
                "finish_time":        time_display,
                "laps":               laps,
                "fastest_lap":        _fmt_lap_time(fl),
                "is_overall_fastest": fl is not None and fl == overall_fastest,
                "elo_value":          row["elo_value"],
                "elo_delta":          row["elo_delta"],
                "current_elo":        row["current_elo"],
            }
        )

    # ── Tire stint data ──────────────────────────────────────────────────────
    conn2 = db_pool.get_conn()
    with conn2.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT steam_id, driver_name, finish_position, race_laps,
                   stint_number, compound_name, lap_start, lap_end, wear_pct
              FROM mart.v_tire_stints
             WHERE session_id = %s
          ORDER BY finish_position NULLS LAST, stint_number;
            """,
            (session_id,),
        )
        stint_rows = cur.fetchall()

    max_race_laps = 0
    driver_order: dict = OrderedDict()
    stints_by_driver: dict = defaultdict(list)
    for row in stint_rows:
        sid_ = row["steam_id"]
        if sid_ not in driver_order:
            driver_order[sid_] = {"name": row["driver_name"], "pos": row["finish_position"]}
        stints_by_driver[sid_].append({
            "stint_number":  row["stint_number"],
            "compound_name": row["compound_name"],
            "lap_start":     row["lap_start"],
            "lap_end":       row["lap_end"],
            "wear_pct":      float(row["wear_pct"] or 0),
        })
        max_race_laps = max(max_race_laps, row["race_laps"] or 0)

    stint_drivers = sorted(
        [
            {
                "steam_id": sid_,
                "name":     info["name"],
                "pos":      info["pos"],
                "stints":   sorted(stints_by_driver[sid_], key=lambda s: s["stint_number"]),
            }
            for sid_, info in driver_order.items()
        ],
        key=lambda d: (d["pos"] or 999),
    )

    return render_template("race_detail.html", meta=meta, results=results,
                           stint_drivers=stint_drivers, max_race_laps=max_race_laps)


# --------------------------------------------------------------------------- #
#  DRIVER PROFILE                                                             #
# --------------------------------------------------------------------------- #
@main_bp.route("/driver/<int:driver_id>")
def driver_profile(driver_id: int):
    """Driver profile: ELO, ELO history chart, flag, last 10 races."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # Driver overview from profile view
        cur.execute(
            """
            SELECT steam_id, driver_name, driver_flag, driver_clan, display_tag,
                   heat_elo, heat_total_races, heat_wins, heat_best_position,
                   heat_last_race_at, event_races, event_wins,
                   hotlap_events, hotlap_total_laps, hotlap_top5,
                   team_tag, fav_track_name, fav_track_url,
                   fav_car_name, fav_car_url, twitch_url, youtube_url
              FROM mart.v_driver_profile
             WHERE steam_id = %s;
            """,
            (driver_id,),
        )
        profile = cur.fetchone()

        if not profile:
            return render_template("driver.html", profile=None, driver_id=driver_id,
                                   elo_chart_json="[]", last_races=[])

        # ELO history (live elo_history entries only; bootstrap is the start point)
        cur.execute(
            """
            SELECT utc_start_time, elo_value, elo_delta, track_name
              FROM mart.v_race_results
             WHERE steam_id = %s
               AND server = 'tripleheat'
               AND elo_value IS NOT NULL
          ORDER BY utc_start_time ASC;
            """,
            (driver_id,),
        )
        elo_rows = cur.fetchall()

        # Last 10 qualifying race participations:
        # - exclude hotlapping
        # - exclude event-server races with < 4 human participants
        # - heats (Tripleheat) and casual_heat always count
        cur.execute(
            """
            SELECT
                session_id,
                utc_start_time,
                server,
                track_name,
                position,
                human_participant_count,
                finish_time,
                laps_completed
              FROM mart.v_race_results
             WHERE steam_id = %s
               AND server != 'hotlapping'
               AND NOT (server = 'events' AND human_participant_count < 4)
          ORDER BY utc_start_time DESC
             LIMIT 10;
            """,
            (driver_id,),
        )
        last_races_raw = cur.fetchall()

    last_races = [
        {
            "session_id":   r["session_id"],
            "date":         r["utc_start_time"].strftime("%d.%m.%y"),
            "server":       r["server"],
            "track_name":   r["track_name"],
            "position":     r["position"],
            "human_count":  r["human_participant_count"],
        }
        for r in last_races_raw
    ]

    # Build ELO chart data: anchor at 1000 start, then one point per race
    elo_chart = []
    if elo_rows:
        elo_chart.append({"label": "Start", "elo": 1000.0})
        for r in elo_rows:
            elo_chart.append({
                "label": r["utc_start_time"].strftime("%d.%m.%y"),
                "elo": round(float(r["elo_value"]), 1),
                "delta": round(float(r["elo_delta"]), 1) if r["elo_delta"] else None,
                "track": r["track_name"],
            })

    return render_template(
        "driver.html",
        profile=profile,
        driver_id=driver_id,
        flag_code=_flag_code(profile["driver_flag"]),
        elo_chart_json=json.dumps(elo_chart),
        last_races=last_races,
    )


# --------------------------------------------------------------------------- #
#  DRIVER PROFILE EDIT                                                        #
# --------------------------------------------------------------------------- #
@main_bp.route("/driver/<int:driver_id>/edit", methods=["POST"])
def driver_profile_edit(driver_id: int):
    """Update editable profile fields for the logged-in driver only."""
    if not g.get("current_steam_id") or g.current_steam_id != driver_id:
        abort(403)
    if not _csrf_ok():
        abort(403)

    conn = db_pool.get_conn()

    # Ensure this steam_id has a racing record (FK requirement).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM mart.v_driver_profile WHERE steam_id = %s",
            (driver_id,),
        )
        if not cur.fetchone():
            abort(404)

    # ── Validate every field before touching the DB ───────────────────────────
    raw_tag = request.form.get("team_tag", "").strip()
    team_tag: str | None = raw_tag or None
    if team_tag and not _TEAM_TAG_RE.fullmatch(team_tag):
        abort(400)

    fav_track_name: str | None = (request.form.get("fav_track_name", "").strip() or None)
    if fav_track_name:
        fav_track_name = fav_track_name[:80]

    fav_track_url: str | None = (request.form.get("fav_track_url", "").strip() or None)
    if fav_track_url and not _WORKSHOP_RE.fullmatch(fav_track_url):
        abort(400)

    fav_car_name: str | None = (request.form.get("fav_car_name", "").strip() or None)
    if fav_car_name:
        fav_car_name = fav_car_name[:80]

    fav_car_url: str | None = (request.form.get("fav_car_url", "").strip() or None)
    if fav_car_url and not _WORKSHOP_RE.fullmatch(fav_car_url):
        abort(400)

    twitch_url: str | None = (request.form.get("twitch_url", "").strip() or None)
    if twitch_url and not _TWITCH_RE.fullmatch(twitch_url):
        abort(400)

    youtube_url: str | None = (request.form.get("youtube_url", "").strip() or None)
    if youtube_url and not _YOUTUBE_RE.fullmatch(youtube_url):
        abort(400)

    # ── Upsert ───────────────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mart.driver_profiles
                (steam_id, team_tag, fav_track_name, fav_track_url,
                 fav_car_name, fav_car_url, twitch_url, youtube_url, updated_at)
            VALUES (%(sid)s, %(tag)s, %(ftn)s, %(ftu)s, %(fcn)s, %(fcu)s,
                    %(tw)s,  %(yt)s,  now())
            ON CONFLICT (steam_id) DO UPDATE SET
                team_tag       = EXCLUDED.team_tag,
                fav_track_name = EXCLUDED.fav_track_name,
                fav_track_url  = EXCLUDED.fav_track_url,
                fav_car_name   = EXCLUDED.fav_car_name,
                fav_car_url    = EXCLUDED.fav_car_url,
                twitch_url     = EXCLUDED.twitch_url,
                youtube_url    = EXCLUDED.youtube_url,
                updated_at     = now()
            """,
            {
                "sid": driver_id,
                "tag": team_tag,
                "ftn": fav_track_name,
                "ftu": fav_track_url,
                "fcn": fav_car_name,
                "fcu": fav_car_url,
                "tw":  twitch_url,
                "yt":  youtube_url,
            },
        )
    conn.commit()

    return redirect(url_for("main.driver_profile", driver_id=driver_id))
