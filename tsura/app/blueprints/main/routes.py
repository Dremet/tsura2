"""Routes for the main (public) blueprint."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import List

import psycopg
import requests
from flask import render_template

from . import main_bp
from ...extensions import db_pool


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
    """Format a positive time gap as '+M:SS.sss' or '+S.sss'."""
    if seconds is None or seconds < 0:
        return "—"
    m = int(seconds // 60)
    s = seconds - m * 60
    if m > 0:
        return f"+{m}:{s:06.3f}"
    return f"+{seconds:.3f}"


# Country name (as stored in DB) → flag emoji via ISO-3166 regional indicators
_FLAG_MAP = {
    "Finland": "🇫🇮", "Germany": "🇩🇪", "United_Kingdom": "🇬🇧",
    "Netherlands": "🇳🇱", "Italy": "🇮🇹", "United_States": "🇺🇸",
    "Belgium": "🇧🇪", "Russia": "🇷🇺", "Brazil": "🇧🇷", "Spain": "🇪🇸",
    "Austria": "🇦🇹", "France": "🇫🇷", "Switzerland": "🇨🇭", "Turkey": "🇹🇷",
    "Czech_Republic": "🇨🇿", "Argentina": "🇦🇷", "Hungary": "🇭🇺",
    "Canada": "🇨🇦", "Portugal": "🇵🇹", "Poland": "🇵🇱", "Greece": "🇬🇷",
    "Guernsey": "🇬🇬", "India": "🇮🇳", "Australia": "🇦🇺", "Ukraine": "🇺🇦",
    "Norway": "🇳🇴", "Serbia": "🇷🇸", "Denmark": "🇩🇰",
    "Bosnia_and_Herzegovina": "🇧🇦", "Malaysia": "🇲🇾", "Ireland": "🇮🇪",
    "Isle_of_Man": "🇮🇲", "Brunei": "🇧🇳", "Cyprus": "🇨🇾", "Kuwait": "🇰🇼",
    "Romania": "🇷🇴", "Afghanistan": "🇦🇫", "Morocco": "🇲🇦", "Chile": "🇨🇱",
    "Sweden": "🇸🇪", "Estonia": "🇪🇪", "Monaco": "🇲🇨", "Thailand": "🇹🇭",
}


def _flag_emoji(flag_text: str | None) -> str:
    if not flag_text:
        return ""
    return _FLAG_MAP.get(flag_text, flag_text.replace("_", " "))


API_URL = (
    "https://api.steampowered.com/IGameServersService/GetServerList/v1/"
    f"?key={os.environ.get('TSURA_STEAM_API_KEY','')}&filter=appid%5C1478340"
)

_RACE_SERVERS = ("events", "heats", "casual_heat")


# --------------------------------------------------------------------------- #
#  INDEX                                                                      #
# --------------------------------------------------------------------------- #
@main_bp.route("/")
def index():
    """Landing page: current hotlap combo and server status."""
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

    servers.sort(key=lambda x: (-x["players"], x["name"].lower()))

    return render_template(
        "index.html",
        servers=servers,
        hotlap=hotlap,
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
        records = cur.fetchall()

    return render_template("elo_heats.html", records=records)


# --------------------------------------------------------------------------- #
#  RACES LIST                                                                 #
# --------------------------------------------------------------------------- #
@main_bp.route("/races")
def races():
    """Race results: per-server last-day summaries + full filtered list."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:

        def _last_day_summary(server: str) -> list:
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

        summary_events = _last_day_summary("events")
        summary_heats = _last_day_summary("heats")
        summary_casual = _last_day_summary("casual_heat")

        # Full list: all race servers, human participants >= 4
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
             WHERE server IN ('events', 'heats', 'casual_heat')
          GROUP BY session_id, utc_start_time, server, track_name
            HAVING MIN(human_participant_count) >= 4
          ORDER BY utc_start_time DESC
             LIMIT 200;
            """
        )
        race_list = cur.fetchall()

    return render_template(
        "races.html",
        races=race_list,
        summary_events=summary_events,
        summary_heats=summary_heats,
        summary_casual=summary_casual,
    )


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
                vehicle_name,
                position,
                finish_time,
                laps_completed,
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
                               meta=None, results=[])

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

        results.append(
            {
                "position":    pos,
                "driver_id":   row["steam_id"],
                "driver_name": row["driver_name"],
                "driver_flag": _flag_emoji(row["driver_flag"]),
                "driver_clan": row["driver_clan"],
                "vehicle":     row["vehicle_name"],
                "finish_time": time_display,
                "laps":        laps,
                "elo_value":   row["elo_value"],
                "elo_delta":   row["elo_delta"],
                "current_elo": row["current_elo"],
            }
        )

    return render_template("race_detail.html", meta=meta, results=results)


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
            SELECT steam_id, driver_name, driver_flag, driver_clan,
                   heat_elo, heat_total_races, heat_wins, heat_best_position,
                   heat_last_race_at, event_races, event_wins,
                   hotlap_events, hotlap_total_laps, hotlap_alltime_best
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
               AND server = 'heats'
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

    # Build ELO chart data: start with bootstrap value, then live entries
    elo_chart = []
    if profile["heat_elo"] is not None and not elo_rows:
        # Only bootstrap, no live history yet
        elo_chart.append({
            "label": "Start",
            "elo": round(profile["heat_elo"], 1),
        })
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
        flag_emoji=_flag_emoji(profile["driver_flag"]),
        elo_chart_json=json.dumps(elo_chart),
        last_races=last_races,
    )
