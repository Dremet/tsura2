-- Expand the fixed palette from four to six options.
ALTER TABLE webadmin.leagues
    DROP CONSTRAINT leagues_color_key_allowed;
ALTER TABLE webadmin.leagues
    ADD CONSTRAINT leagues_color_key_allowed
    CHECK (color_key IN ('coral', 'teal', 'gold', 'violet', 'blue', 'lime'));
