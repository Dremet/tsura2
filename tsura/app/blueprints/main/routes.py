"""Routes for the main (public) blueprint."""

from __future__ import annotations

import os
from decimal import Decimal
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


API_URL = (
    "https://api.steampowered.com/IGameServersService/GetServerList/v1/"
    f"?key={os.environ.get('TSURA_STEAM_API_KEY','')}&filter=appid%5C1478340"
)


@main_bp.route("/")
def index():
    """Landing page showing recent races, current hotlap combo and server status."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # recent races ---------------------------------------------------
        cur.execute(
            """
            SELECT e_id,
                   timestamp,
                   track,
                   participants,
                   cars,
                   winner
              FROM tsu.mart.fact_recent_races
          ORDER BY timestamp DESC
             LIMIT 20;
            """
        )
        races = cur.fetchall()

        # current hotlapping combo --------------------------------------
        cur.execute(
            """
            SELECT h_h_id,
                   tr_name,
                   event_start,
                   cars_used,
                   number_of_race_results
              FROM tsu.mart.fact_hotlapping_list
          ORDER BY h_h_id DESC
             LIMIT 1;
            """
        )
        hotlap = cur.fetchone()

    # server list -------------------------------------------------------
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


@main_bp.route("/hotlapping")
def hotlapping():
    """Page listing all hotlapping combinations."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT h_h_id,
                   tr_name,
                   event_start,
                   cars_used,
                   number_of_race_results
              FROM tsu.mart.fact_hotlapping_list
          ORDER BY h_h_id DESC;
            """
        )
        events = cur.fetchall()

    return render_template("hotlapping.html", events=events)


# --------------------------------------------------------------------------- #
#  HOTLAPPING DETAIL                                                          #
# --------------------------------------------------------------------------- #
@main_bp.route("/hotlapping/<int:event_number>")
def hotlapping_detail(event_number: int):
    """Show best lap per driver and top‑500 laps for a given hot‑lap event."""
    conn = db_pool.get_conn()

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # best lap per driver -------------------------------------------------
        cur.execute(
            """
            SELECT
                d_d_id,                   -- driver id (hash)
                d_name,
                v_name,
                e_timestamp,
                h_best_lap_time,
                h_diff_to_best_lap,
                h_is_consistent,
                h_is_very_consistent,
                s_times                   -- ARRAY[sector times]
            FROM tsu.mart.fact_hotlapping_results_best
            WHERE h_id = %s
            and h_diff_to_best_lap is not null
        ORDER BY h_best_lap_time desc
            """,
            (event_number,),
        )
        best_rows = cur.fetchall()

        # overall top‑500 laps -----------------------------------------------
        cur.execute(
            """
            SELECT d_d_id,
                   d_name,
                   v_name,
                   e_timestamp,
                   h_lap_time,
                   s_times
              FROM tsu.mart.fact_hotlapping_results_all
             WHERE h_id = %s
             and h_lap_time is not null
          ORDER BY h_lap_time
             LIMIT 500;
            """,
            (event_number,),
        )
        lap_rows = cur.fetchall()

    # ------------------------------------------------------------------ #
    # compute sector bests & theoretical optimal lap                     #
    # ------------------------------------------------------------------ #
    n_sectors: int = len(best_rows[0]["s_times"])
    best_sector_vals: List[float] = [float("inf")] * n_sectors
    best_sector_drivers: List[str] = [""] * n_sectors

    def check_sectors(row):
        for idx, sec_time in enumerate(row["s_times"]):
            try:
                t = float(sec_time)
                if t < best_sector_vals[idx]:
                    best_sector_vals[idx] = t
                    best_sector_drivers[idx] = row["d_name"]
            except:
                continue

    for r in best_rows:
        check_sectors(r)
    for r in lap_rows:
        check_sectors(r)

    optimal_lap_sec = sum(best_sector_vals)
    best_real_sec = float(lap_rows[0]["h_lap_time"])
    optimal_diff = optimal_lap_sec - best_real_sec

    best_sector_fmt = [_fmt_lap_time(t) for t in best_sector_vals]
    fastest_lap_fmt = _fmt_lap_time(best_rows[0]["h_best_lap_time"])

    # ------------------------------------------------------------------ #
    # prepare context for Jinja                                          #
    # ------------------------------------------------------------------ #
    best = [
        {
            "driver_id": row["d_d_id"],
            "driver": row["d_name"],
            "car": row["v_name"],
            "lap": _fmt_lap_time(
                float(best_rows[0]["h_best_lap_time"])
                + float(row["h_diff_to_best_lap"] or 0)
            ),
            "diff": (
                "-"
                if row["h_diff_to_best_lap"] in (None, 0, Decimal(0))
                else f"+{row['h_diff_to_best_lap']:.4f}"
            ),
            "consistent": row["h_is_consistent"],
            "very_consistent": row["h_is_very_consistent"],
            "sectors": [_fmt_lap_time(t) for t in row["s_times"]],
            "ts": row["e_timestamp"],
        }
        for row in best_rows
    ]

    laps = [
        {
            "driver_id": row["d_d_id"],
            "driver": row["d_name"],
            "car": row["v_name"],
            "lap": _fmt_lap_time(row["h_lap_time"]),
            "sectors": [_fmt_lap_time(t) for t in row["s_times"]],
            "ts": row["e_timestamp"],
        }
        for row in lap_rows
    ]

    for row in best_rows:
        print(row["d_name"], row["h_best_lap_time"])

    return render_template(
        "hotlapping_detail.html",
        event_number=event_number,
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
#  ELO EVENTS                                                                 #
# --------------------------------------------------------------------------- #
@main_bp.route("/elo-events")
def elo_events():
    """Leaderboard for ELO‑rated event races (min. 3 participations)."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT d_d_id,
                   d_name,
                   e_e_id,
                   ee_current_elo,
                   ee_elo_delta,
                   ee_elo_delta_5,
                   ee_participations
              FROM tsu.mart.fact_elo_events
             WHERE ee_participations >= 3
          ORDER BY ee_current_elo DESC;
            """
        )
        records = cur.fetchall()

    return render_template("elo_events.html", records=records)


# --------------------------------------------------------------------------- #
#  ELO HEATS                                                                  #
# --------------------------------------------------------------------------- #
@main_bp.route("/elo-heats")
def elo_heats():
    """Leaderboard for ELO‑rated heat races (min. 3 participations)."""
    conn = db_pool.get_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT d_d_id,
                   d_name,
                   e_e_id,
                   ee_current_elo,
                   ee_elo_delta,
                   ee_elo_delta_6,
                   ee_participations
              FROM tsu.mart.fact_elo_heats
             WHERE ee_participations >= 3
          ORDER BY ee_current_elo DESC;
            """
        )
        records = cur.fetchall()

    return render_template("elo_heats.html", records=records)


@main_bp.route("/events/<event_id>")
def event_detail(event_id):
    """Placeholder for the event‑detail view."""
    return f"Event detail for {event_id} coming soon!", 200


@main_bp.route("/driver/<driver_id>")
def driver_profile(driver_id: str):
    """Placeholder page for a driver profile (hash ID)."""
    return f"Driver profile for ID {driver_id} – coming soon!", 200
