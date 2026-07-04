-- 008_career_beta.sql — beta-phase controls.
-- Run as the schema owner (prod: `data`; test: `tsu_dev`). Additive.

-- Allowlist: during the test phase only these Steam IDs may join TSU Career
-- and see the garage. Managed by admins in the website.
CREATE TABLE IF NOT EXISTS career.allowed_participants (
    steam_id  BIGINT PRIMARY KEY,
    note      TEXT,
    added_by  BIGINT,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Allow deleting a whole season (cascades its rewards too). The other child
-- tables already cascade; race_rewards was created without it.
ALTER TABLE career.race_rewards DROP CONSTRAINT IF EXISTS race_rewards_season_id_fkey;
ALTER TABLE career.race_rewards
    ADD CONSTRAINT race_rewards_season_id_fkey
    FOREIGN KEY (season_id) REFERENCES career.seasons(id) ON DELETE CASCADE;

-- Allowlist with resolved driver names for the admin UI.
CREATE OR REPLACE VIEW mart.v_career_participants AS
SELECT ap.steam_id, d.name AS driver_name, ap.note, ap.added_by, ap.added_at
FROM career.allowed_participants ap
LEFT JOIN base.drivers d ON d.steam_id = ap.steam_id;

GRANT SELECT, INSERT, UPDATE, DELETE ON career.allowed_participants TO tsura;
GRANT SELECT ON mart.v_career_participants TO tsura;
