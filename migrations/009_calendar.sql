-- Public race calendar. Run as the webadmin schema owner before deploying the site.
CREATE TABLE IF NOT EXISTS webadmin.leagues (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name varchar(80) NOT NULL CHECK (length(btrim(name)) > 0),
    description varchar(240) NOT NULL DEFAULT '',
    created_by bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS leagues_name_ci
    ON webadmin.leagues (lower(name));

CREATE TABLE IF NOT EXISTS webadmin.calendar_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    league_id bigint NOT NULL REFERENCES webadmin.leagues(id) ON DELETE RESTRICT,
    details varchar(160) NOT NULL CHECK (length(btrim(details)) > 0),
    starts_at timestamptz NOT NULL,
    created_by bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calendar_events_starts_at
    ON webadmin.calendar_events (starts_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON webadmin.leagues, webadmin.calendar_events TO tsura;
GRANT USAGE ON SEQUENCE webadmin.leagues_id_seq, webadmin.calendar_events_id_seq TO tsura;
