"""Routes for the main (public) blueprint."""

from __future__ import annotations

import os
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


API_URL = (
    "https://api.steampowered.com/IGameServersService/GetServerList/v1/"
    f"?key={os.environ.get('TSURA_STEAM_API_KEY','')}&filter=appid%5C1478340"
)


# --------------------------------------------------------------------------- #
#  INDEX                                                                      #
# --------------------------------------------------------------------------- #
@main_bp.route("/")
def index():
    """Landing page: recent races, current hotlap combo, server status."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # recent races (events + heats, aggregated to one row per session) --
        cur.execute(
            """
            SELECT
                session_id,
                utc_start_time,
                server,
                track_name,
                participant_count,
                STRING_AGG(DISTINCT vehicle_name, ', ' ORDER BY vehicle_name) AS cars,
                MIN(driver_name) FILTER (WHERE position = 1) AS winner
              FROM mart.v_race_results
             WHERE server IN ('events', 'heats')
          GROUP BY session_id, utc_start_time, server, track_name, participant_count
          ORDER BY utc_start_time DESC
             LIMIT 20;
            """
        )
        races = cur.fetchall()

        # most recent hotlap session (combo card) ----------------------------
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
        races=races,
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
        # best lap per driver across the whole session ----------------------
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

        # top-500 laps across the whole session -----------------------------
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
            "consistent": False,
            "very_consistent": False,
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
    """Race results list: events + heats, one row per session."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT
                session_id,
                utc_start_time,
                server,
                track_name,
                participant_count,
                STRING_AGG(DISTINCT vehicle_name, ', ' ORDER BY vehicle_name) AS cars,
                MIN(driver_name) FILTER (WHERE position = 1) AS winner
              FROM mart.v_race_results
             WHERE server IN ('events', 'heats')
          GROUP BY session_id, utc_start_time, server, track_name, participant_count
          ORDER BY utc_start_time DESC
             LIMIT 200;
            """
        )
        race_list = cur.fetchall()

    return render_template("races.html", races=race_list)


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
                participant_count,
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
        "session_id":      rows[0]["session_id"],
        "utc_start_time":  rows[0]["utc_start_time"],
        "server":          rows[0]["server"],
        "track_name":      rows[0]["track_name"],
        "track_type":      rows[0]["track_type"],
        "participant_count": rows[0]["participant_count"],
        "finished_state":  rows[0]["finished_state"],
    }

    results = [
        {
            "position":      row["position"],
            "driver_id":     row["steam_id"],
            "driver_name":   row["driver_name"],
            "driver_flag":   row["driver_flag"],
            "driver_clan":   row["driver_clan"],
            "vehicle":       row["vehicle_name"],
            "finish_time":   _fmt_race_time(row["finish_time"]),
            "laps":          row["laps_completed"],
            "elo_value":     row["elo_value"],
            "elo_delta":     row["elo_delta"],
            "current_elo":   row["current_elo"],
        }
        for row in rows
    ]

    return render_template("race_detail.html", meta=meta, results=results)


@main_bp.route("/driver/<driver_id>")
def driver_profile(driver_id: str):
    """Placeholder page for a driver profile."""
    return f"Driver profile for ID {driver_id} - coming soon!", 200
