-- 007_career.sql — Career mode schema.
--
-- Run as the schema owner (prod: user `data`; test: `tsu_dev`). Creates the
-- `career` schema (config + player state, written by BOTH the website role
-- `tsura` and the pipeline role `data`) plus mart views that expose career
-- data (which joins base.* race results) read-only to `tsura`.
--
-- Design notes:
--  * Career race RESULTS live in base.race_sessions / base.race_participations
--    with server='career' (written by the pipeline like any other category);
--    they get NO ELO (update_elo is gated to server='tripleheat').
--  * There is no qualifying flag in the game data. Quali runs as a Hotlapping
--    session whose stats are discarded, so it only sets the race grid. Hence
--    "pole / quali win" is derived from race start_position = 1, and
--    "fastest lap" from the min fastest_lap in the race — both already in base.
--  * Two write actors: the website (role `tsura`) manages seasons, enrollments
--    and upgrades; the pipeline (role `data`) computes per-race rewards.

CREATE SCHEMA IF NOT EXISTS career;

-- ---------------------------------------------------------------- seasons
-- One season = one base Workshop car + credit economy. Exactly one active.
CREATE TABLE IF NOT EXISTS career.seasons (
    id                SERIAL PRIMARY KEY,
    name              TEXT NOT NULL,
    base_vehicle_name TEXT NOT NULL,          -- in-game choosable name
    base_vehicle_veh  TEXT NOT NULL,          -- path to the base .veh on disk
    start_credits     INT  NOT NULL DEFAULT 0,
    credit_first      INT  NOT NULL,          -- credits awarded for P1
    credit_last       INT  NOT NULL,          -- credits for last place (>= first)
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'active', 'finished')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at      TIMESTAMPTZ,            -- set when status -> active
    finished_at       TIMESTAMPTZ            -- set when status -> finished
);
-- at most one active season at a time
CREATE UNIQUE INDEX IF NOT EXISTS career_one_active_season
    ON career.seasons ((status = 'active')) WHERE status = 'active';

-- ------------------------------------------------------------ upgrade_axes
-- Per-season tunable axes + economics. base_value is seeded from the base car
-- (the four axes map to .veh physics fields: top_speed->speed.maxSpeed,
--  acceleration->speed.acceleration, braking->braking.braking,
--  downforce->downforce.downforce). final value = base_value + tier*step_per_tier.
CREATE TABLE IF NOT EXISTS career.upgrade_axes (
    season_id     INT  NOT NULL REFERENCES career.seasons(id) ON DELETE CASCADE,
    axis          TEXT NOT NULL
                  CHECK (axis IN ('top_speed', 'acceleration', 'braking', 'downforce')),
    base_value    DOUBLE PRECISION NOT NULL,
    step_per_tier DOUBLE PRECISION NOT NULL,
    max_tier      INT  NOT NULL DEFAULT 5 CHECK (max_tier >= 0),
    cost_per_tier INT  NOT NULL CHECK (cost_per_tier >= 0),
    PRIMARY KEY (season_id, axis)
);

-- -------------------------------------------------------------- enrollments
-- A driver joins a season. start_credits granted at join; races before
-- joined_at are back-filled with mid-field credits (see v_career_credit_balance).
CREATE TABLE IF NOT EXISTS career.enrollments (
    season_id INT    NOT NULL REFERENCES career.seasons(id) ON DELETE CASCADE,
    steam_id  BIGINT NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (season_id, steam_id)
);

-- ---------------------------------------------------------- driver_upgrades
-- Current upgrade tier per driver/axis/season (website writes on purchase).
CREATE TABLE IF NOT EXISTS career.driver_upgrades (
    season_id  INT    NOT NULL REFERENCES career.seasons(id) ON DELETE CASCADE,
    steam_id   BIGINT NOT NULL,
    axis       TEXT   NOT NULL
               CHECK (axis IN ('top_speed', 'acceleration', 'braking', 'downforce')),
    tier       INT    NOT NULL DEFAULT 0 CHECK (tier >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (season_id, steam_id, axis)
);

-- ------------------------------------------------------------- race_rewards
-- Pipeline-computed per-race reward, one row per human participation in a
-- career race. Idempotent upsert on participation_id (like base.elo_history).
CREATE TABLE IF NOT EXISTS career.race_rewards (
    participation_id TEXT PRIMARY KEY
                     REFERENCES base.race_participations(id) ON DELETE CASCADE,
    session_id       TEXT   NOT NULL REFERENCES base.race_sessions(id) ON DELETE CASCADE,
    season_id        INT    REFERENCES career.seasons(id),
    steam_id         BIGINT NOT NULL,
    credits          INT    NOT NULL DEFAULT 0,   -- position-based, slower = more
    points_finish    INT    NOT NULL DEFAULT 0,   -- championship points from position
    is_pole          BOOLEAN NOT NULL DEFAULT false,  -- start_position = 1 (quali win)
    is_fastest_lap   BOOLEAN NOT NULL DEFAULT false,  -- owns the race's fastest lap
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS career_rewards_season_driver
    ON career.race_rewards(season_id, steam_id);

-- -------------------------------------------------------------------- admins
CREATE TABLE IF NOT EXISTS career.admins (
    steam_id BIGINT PRIMARY KEY,
    note     TEXT
);
INSERT INTO career.admins(steam_id, note)
VALUES (76561197989276622, 'André / Dremet')
ON CONFLICT (steam_id) DO NOTHING;

-- =================================================================== VIEWS

-- Map each career race session to the season active at its start time.
CREATE OR REPLACE VIEW mart.v_career_race_sessions AS
SELECT rs.id AS session_id, rs.utc_start_time, rs.track_guid,
       t.name AS track_name, rs.max_laps, rs.participant_count,
       s.id AS season_id, s.name AS season_name
FROM base.race_sessions rs
JOIN base.tracks t ON rs.track_guid = t.guid
LEFT JOIN career.seasons s
       ON rs.utc_start_time >= s.activated_at
      AND rs.utc_start_time <  COALESCE(s.finished_at, 'infinity'::timestamptz)
WHERE rs.server = 'career';

-- One row per human participant in a career race (results / race detail pages).
CREATE OR REPLACE VIEW mart.v_career_results AS
SELECT rp.id AS participation_id, rs.id AS session_id, rs.utc_start_time,
       t.name AS track_name, t.level_type AS track_type,
       rp.steam_id, d.name AS driver_name, d.flag AS driver_flag,
       COALESCE(dp.team_tag, d.clan) AS display_tag,
       rp.position, rp.start_position,
       rp.finish_time - COALESCE(rs.race_start_offset_s, 0) AS finish_time,
       rp.laps_completed, rp.fastest_lap, rs.participant_count,
       cr.season_id, cr.credits, cr.points_finish, cr.is_pole, cr.is_fastest_lap,
       (cr.points_finish + cr.is_pole::int + cr.is_fastest_lap::int) AS points_total
FROM base.race_participations rp
JOIN base.race_sessions rs ON rp.session_id = rs.id
JOIN base.tracks t         ON rs.track_guid = t.guid
JOIN base.drivers d        ON rp.steam_id = d.steam_id
LEFT JOIN mart.driver_profiles dp ON dp.steam_id = rp.steam_id
LEFT JOIN career.race_rewards cr  ON cr.participation_id = rp.id
WHERE rs.server = 'career' AND rp.is_ai = false;

-- Season standings: championship points per driver.
CREATE OR REPLACE VIEW mart.v_career_standings AS
SELECT cr.season_id, cr.steam_id, d.name AS driver_name, d.flag AS driver_flag,
       COALESCE(dp.team_tag, d.clan) AS display_tag,
       COUNT(*) AS races,
       SUM(CASE WHEN rp.position = 1 THEN 1 ELSE 0 END) AS wins,
       SUM(cr.points_finish) AS points_finish,
       SUM(cr.is_pole::int) AS poles,
       SUM(cr.is_fastest_lap::int) AS fastest_laps,
       SUM(cr.points_finish + cr.is_pole::int + cr.is_fastest_lap::int) AS points_total
FROM career.race_rewards cr
JOIN base.race_participations rp ON rp.id = cr.participation_id
JOIN base.drivers d              ON d.steam_id = cr.steam_id
LEFT JOIN mart.driver_profiles dp ON dp.steam_id = cr.steam_id
GROUP BY cr.season_id, cr.steam_id, d.name, d.flag, COALESCE(dp.team_tag, d.clan);

-- Upgrade overview (rows = drivers, one row per axis) + resolved tuned value.
CREATE OR REPLACE VIEW mart.v_career_upgrades AS
SELECT du.season_id, du.steam_id, d.name AS driver_name,
       du.axis, du.tier, ua.base_value, ua.step_per_tier, ua.max_tier,
       ua.cost_per_tier,
       ua.base_value + du.tier * ua.step_per_tier AS final_value,
       du.tier * ua.cost_per_tier AS spent
FROM career.driver_upgrades du
JOIN career.upgrade_axes ua ON ua.season_id = du.season_id AND ua.axis = du.axis
JOIN base.drivers d         ON d.steam_id = du.steam_id;

-- Credit balance per enrolled driver: start grant + earned + mid-field backfill
-- for races missed before joining - spent on upgrades.
CREATE OR REPLACE VIEW mart.v_career_credit_balance AS
WITH earned AS (
    SELECT season_id, steam_id, COALESCE(SUM(credits), 0) AS earned
    FROM career.race_rewards GROUP BY season_id, steam_id
),
spent AS (
    SELECT du.season_id, du.steam_id,
           COALESCE(SUM(du.tier * ua.cost_per_tier), 0) AS spent
    FROM career.driver_upgrades du
    JOIN career.upgrade_axes ua ON ua.season_id = du.season_id AND ua.axis = du.axis
    GROUP BY du.season_id, du.steam_id
),
races_before AS (
    SELECT e.season_id, e.steam_id,
           (SELECT COUNT(*) FROM base.race_sessions rs
             WHERE rs.server = 'career'
               AND rs.utc_start_time >= s.activated_at
               AND rs.utc_start_time <  COALESCE(s.finished_at, 'infinity'::timestamptz)
               AND rs.utc_start_time <  e.joined_at) AS races_missed
    FROM career.enrollments e JOIN career.seasons s ON s.id = e.season_id
)
SELECT e.season_id, e.steam_id,
       s.start_credits,
       COALESCE(ea.earned, 0) AS earned,
       rb.races_missed,
       (ROUND((s.credit_first + s.credit_last) / 2.0)::int * rb.races_missed) AS backfill,
       COALESCE(sp.spent, 0) AS spent,
       s.start_credits + COALESCE(ea.earned, 0)
         + (ROUND((s.credit_first + s.credit_last) / 2.0)::int * rb.races_missed)
         - COALESCE(sp.spent, 0) AS balance
FROM career.enrollments e
JOIN career.seasons s      ON s.id = e.season_id
JOIN races_before rb       ON rb.season_id = e.season_id AND rb.steam_id = e.steam_id
LEFT JOIN earned ea        ON ea.season_id = e.season_id AND ea.steam_id = e.steam_id
LEFT JOIN spent  sp        ON sp.season_id = e.season_id AND sp.steam_id = e.steam_id;

-- Per-driver car setup for the active-season vehicle generator: one row per
-- enrolled driver per axis, resolved final value (tier 0 default) + driver name.
CREATE OR REPLACE VIEW mart.v_career_driver_cars AS
SELECT e.season_id, e.steam_id, d.name AS driver_name, ua.axis,
       COALESCE(du.tier, 0) AS tier,
       ua.base_value + COALESCE(du.tier, 0) * ua.step_per_tier AS final_value
FROM career.enrollments e
JOIN base.drivers d          ON d.steam_id = e.steam_id
JOIN career.upgrade_axes ua  ON ua.season_id = e.season_id
LEFT JOIN career.driver_upgrades du
       ON du.season_id = e.season_id AND du.steam_id = e.steam_id AND du.axis = ua.axis;

-- =================================================================== GRANTS
GRANT USAGE ON SCHEMA career TO tsura;
GRANT SELECT ON ALL TABLES IN SCHEMA career TO tsura;
-- website-managed tables: full write
GRANT SELECT, INSERT, UPDATE, DELETE ON
      career.seasons, career.upgrade_axes, career.enrollments, career.driver_upgrades
   TO tsura;
GRANT USAGE, SELECT ON SEQUENCE career.seasons_id_seq TO tsura;
-- mart views
GRANT SELECT ON mart.v_career_race_sessions, mart.v_career_results,
                mart.v_career_standings, mart.v_career_upgrades,
                mart.v_career_credit_balance, mart.v_career_driver_cars
   TO tsura;
