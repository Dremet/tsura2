-- Start position on race detail page.
-- Run against prod DB as the admin/data user. Safe to re-run.
--
-- base.race_participations.start_position is filled by the pipeline for ALL
-- race sessions (the game writes players[].startPosition into every
-- *_event.json) — this migration only exposes it through the view.
--
-- NOTE: CREATE OR REPLACE VIEW requires all NEW columns to be appended at the
-- end of the existing column list (currently ends with display_tag from
-- 005_login.sql).

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
    COALESCE(dp.team_tag, d.clan) AS display_tag,
    -- NEW: grid position from players[].startPosition
    rp.start_position
FROM base.race_participations rp
JOIN base.race_sessions rs      ON rp.session_id = rs.id
JOIN base.tracks t              ON rs.track_guid = t.guid
JOIN base.drivers d             ON rp.steam_id = d.steam_id
LEFT JOIN base.vehicles v       ON rp.vehicle_guid = v.guid
LEFT JOIN base.elo_history eh   ON rp.id = eh.participation_id
LEFT JOIN base.elo_bootstrap eb ON rp.steam_id = eb.steam_id
LEFT JOIN mart.driver_profiles dp ON dp.steam_id = rp.steam_id
WHERE rp.is_ai = false;

GRANT SELECT ON mart.v_race_results TO tsura;
