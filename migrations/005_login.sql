-- Phase 4: Steam login + driver profiles
-- Run against prod DB as the admin/data user.
-- Safe to re-run: IF NOT EXISTS + CREATE OR REPLACE.
--
-- Tables live in mart.* (not base.*) because tsura user already has USAGE
-- on mart schema; base schema is owned by postgres and not directly accessible
-- to tsura. data user owns mart objects and can grant to tsura directly.
--
-- NOTE: CREATE OR REPLACE VIEW requires all NEW columns to be appended at the
-- end of an existing view's column list.

-- ── New tables in mart schema ────────────────────────────────────────────────

-- No FK to base.drivers: non-driver visitors may also log in via Steam.
-- Application enforces that only drivers (rows in v_driver_profile) can edit.
CREATE TABLE IF NOT EXISTS mart.driver_profiles (
    steam_id        BIGINT PRIMARY KEY,
    team_tag        TEXT,        -- max 3 alphanumeric chars, NULL = not set
    fav_track_name  TEXT,
    fav_track_url   TEXT,        -- Steam Workshop URL
    fav_car_name    TEXT,
    fav_car_url     TEXT,        -- Steam Workshop URL
    twitch_url      TEXT,
    youtube_url     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mart.user_sessions (
    session_id  TEXT        PRIMARY KEY,
    steam_id    BIGINT      NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- ── Grants: tsura user gets write ONLY on the two new tables ─────────────────
-- Read-only access to all race/elo/hotlap data is unchanged.

GRANT SELECT, INSERT, UPDATE, DELETE ON mart.driver_profiles TO tsura;
GRANT SELECT, INSERT, UPDATE, DELETE ON mart.user_sessions   TO tsura;

-- Explicitly revoke write from tsura on core data tables (no-op if not granted).
-- base schema is not accessible to tsura anyway, but this is explicit documentation.
REVOKE INSERT, UPDATE, DELETE ON base.drivers             FROM tsura;
REVOKE INSERT, UPDATE, DELETE ON base.race_sessions       FROM tsura;
REVOKE INSERT, UPDATE, DELETE ON base.race_participations FROM tsura;
REVOKE INSERT, UPDATE, DELETE ON base.elo_history         FROM tsura;
REVOKE INSERT, UPDATE, DELETE ON base.hotlap_events       FROM tsura;
REVOKE INSERT, UPDATE, DELETE ON base.hotlap_laps         FROM tsura;
REVOKE INSERT, UPDATE, DELETE ON base.elo_bootstrap       FROM tsura;

-- ── Updated mart views ────────────────────────────────────────────────────────
-- All EXISTING columns stay in their original order; NEW columns are appended.
-- This satisfies PostgreSQL's CREATE OR REPLACE VIEW constraint.

-- v_race_results: append display_tag at end
CREATE OR REPLACE VIEW mart.v_race_results AS
SELECT
    rp.id              AS participation_id,
    rs.id              AS session_id,
    rs.utc_start_time,
    rs.server,
    rs.finished_state,
    rs.track_guid,
    t.name             AS track_name,
    t.level_type       AS track_type,
    rp.steam_id,
    d.name             AS driver_name,
    d.flag             AS driver_flag,
    d.clan             AS driver_clan,
    rp.vehicle_guid,
    v.name             AS vehicle_name,
    rp.position,
    rp.finish_time - COALESCE(rs.race_start_offset_s, 0) AS finish_time,
    rp.laps_completed,
    rs.participant_count,
    eh.elo_value,
    eh.elo_delta,
    COALESCE(
        (SELECT eh2.elo_value
         FROM base.elo_history eh2
         JOIN base.race_participations rp2 ON rp2.id = eh2.participation_id
         JOIN base.race_sessions rs2 ON rs2.id = rp2.session_id
         WHERE rp2.steam_id = rp.steam_id
         ORDER BY rs2.utc_start_time DESC
         LIMIT 1),
        eb.elo_value
    ) AS current_elo,
    COUNT(*) OVER (PARTITION BY rs.id) AS human_participant_count,
    rp.fastest_lap,
    -- NEW: team tag overrides clan for display
    COALESCE(dp.team_tag, d.clan) AS display_tag
FROM base.race_participations rp
JOIN base.race_sessions rs      ON rp.session_id = rs.id
JOIN base.tracks t              ON rs.track_guid = t.guid
JOIN base.drivers d             ON rp.steam_id = d.steam_id
LEFT JOIN base.vehicles v       ON rp.vehicle_guid = v.guid
LEFT JOIN base.elo_history eh   ON rp.id = eh.participation_id
LEFT JOIN base.elo_bootstrap eb ON rp.steam_id = eb.steam_id
LEFT JOIN mart.driver_profiles dp ON dp.steam_id = rp.steam_id
WHERE rp.is_ai = false;


-- v_driver_profile: append new profile fields + display_tag at end
CREATE OR REPLACE VIEW mart.v_driver_profile AS
WITH elo_current AS (
    SELECT
        d.steam_id,
        COALESCE(
            (SELECT eh.elo_value
             FROM base.elo_history eh
             JOIN base.race_participations rp ON rp.id = eh.participation_id
             JOIN base.race_sessions rs ON rs.id = rp.session_id
             WHERE rp.steam_id = d.steam_id AND rs.server = 'tripleheat'
             ORDER BY rs.utc_start_time DESC
             LIMIT 1),
            eb.elo_value
        ) AS elo_value,
        eb.number_races AS legacy_race_count,
        eb.last_race_at AS legacy_last_race
    FROM base.drivers d
    LEFT JOIN base.elo_bootstrap eb ON eb.steam_id = d.steam_id
),
elo_ranked AS (
    SELECT
        rp.steam_id,
        eh.elo_delta,
        ROW_NUMBER() OVER (
            PARTITION BY rp.steam_id
            ORDER BY rs.utc_start_time DESC
        ) AS rn
    FROM base.elo_history eh
    JOIN base.race_participations rp ON rp.id = eh.participation_id
    JOIN base.race_sessions rs       ON rs.id = rp.session_id
    WHERE rs.server = 'tripleheat'
),
elo_last AS (
    SELECT steam_id, elo_delta AS heat_elo_delta
    FROM elo_ranked
    WHERE rn = 1
),
elo_trend AS (
    SELECT steam_id, SUM(elo_delta) AS heat_elo_trend_6
    FROM elo_ranked
    WHERE rn <= 6
    GROUP BY steam_id
),
heat_stats AS (
    SELECT
        rp.steam_id,
        COUNT(*)                          AS heat_races,
        SUM(CASE WHEN rp.position = 1 THEN 1 ELSE 0 END) AS heat_wins,
        MIN(rp.position)                  AS heat_best_position,
        MAX(rs.utc_start_time)            AS heat_last_race_at
    FROM base.race_participations rp
    JOIN base.race_sessions rs ON rs.id = rp.session_id
    WHERE rs.server = 'tripleheat'
      AND rp.is_ai = false
      AND rs.utc_start_time > COALESCE(
          (SELECT MAX(last_race_at) FROM base.elo_bootstrap),
          '-infinity'::timestamptz
      )
    GROUP BY rp.steam_id
),
event_stats AS (
    SELECT
        rp.steam_id,
        COUNT(*)                          AS event_races,
        SUM(CASE WHEN rp.position = 1 THEN 1 ELSE 0 END) AS event_wins,
        MAX(rs.utc_start_time)            AS event_last_race_at
    FROM base.race_participations rp
    JOIN base.race_sessions rs ON rs.id = rp.session_id
    WHERE rs.server = 'events' AND rp.is_ai = false
    GROUP BY rp.steam_id
),
hotlap_stats AS (
    SELECT
        hl.steam_id,
        COUNT(DISTINCT he.id)             AS hotlap_events,
        COUNT(*)                          AS hotlap_total_laps,
        MIN(hl.lap_time)                  AS hotlap_alltime_best,
        MAX(he.utc_start_time)            AS hotlap_last_session_at
    FROM base.hotlap_laps hl
    JOIN base.hotlap_events he ON he.id = hl.event_id
    GROUP BY hl.steam_id
),
hotlap_top5_stats AS (
    SELECT steam_id, COUNT(*) AS hotlap_top5
    FROM (
        SELECT
            group_id,
            steam_id,
            RANK() OVER (PARTITION BY group_id ORDER BY MIN(lap_time)) AS rnk
        FROM mart.v_hotlap_group_results
        GROUP BY group_id, steam_id
    ) sub
    WHERE rnk <= 5
    GROUP BY steam_id
)
SELECT
    d.steam_id,
    d.name             AS driver_name,
    d.flag             AS driver_flag,
    d.clan             AS driver_clan,
    ec.elo_value       AS heat_elo,
    COALESCE(hs.heat_races, 0) + COALESCE(ec.legacy_race_count, 0) AS heat_total_races,
    COALESCE(hs.heat_races, 0) AS heat_races_new_pipeline,
    COALESCE(hs.heat_wins,  0) AS heat_wins,
    hs.heat_best_position,
    GREATEST(hs.heat_last_race_at, ec.legacy_last_race) AS heat_last_race_at,
    COALESCE(es.event_races, 0) AS event_races,
    COALESCE(es.event_wins,  0) AS event_wins,
    es.event_last_race_at,
    COALESCE(hls.hotlap_events,     0) AS hotlap_events,
    COALESCE(hls.hotlap_total_laps, 0) AS hotlap_total_laps,
    hls.hotlap_alltime_best,
    hls.hotlap_last_session_at,
    el.heat_elo_delta,
    et.heat_elo_trend_6,
    COALESCE(ht5.hotlap_top5, 0) AS hotlap_top5,
    -- NEW: editable profile fields + display tag (appended to preserve column order)
    dp.team_tag,
    dp.fav_track_name,
    dp.fav_track_url,
    dp.fav_car_name,
    dp.fav_car_url,
    dp.twitch_url,
    dp.youtube_url,
    COALESCE(dp.team_tag, d.clan) AS display_tag
FROM base.drivers d
LEFT JOIN elo_current   ec  ON ec.steam_id  = d.steam_id
LEFT JOIN elo_last      el  ON el.steam_id  = d.steam_id
LEFT JOIN elo_trend     et  ON et.steam_id  = d.steam_id
LEFT JOIN heat_stats    hs  ON hs.steam_id  = d.steam_id
LEFT JOIN event_stats   es  ON es.steam_id  = d.steam_id
LEFT JOIN hotlap_stats      hls ON hls.steam_id = d.steam_id
LEFT JOIN hotlap_top5_stats ht5 ON ht5.steam_id = d.steam_id
LEFT JOIN mart.driver_profiles dp ON dp.steam_id = d.steam_id;


-- Re-grant SELECT on updated views.
GRANT SELECT ON mart.v_race_results   TO tsura;
GRANT SELECT ON mart.v_driver_profile TO tsura;
