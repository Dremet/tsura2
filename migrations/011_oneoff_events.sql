-- Standalone races have no league. Existing league events are unchanged.
ALTER TABLE webadmin.calendar_events
    ALTER COLUMN league_id DROP NOT NULL;
