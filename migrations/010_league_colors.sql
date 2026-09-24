-- Let league admins pick a color from the four calendar accents.
-- Existing leagues keep their current id-based color; reruns keep later choices.
ALTER TABLE webadmin.leagues ADD COLUMN IF NOT EXISTS color_key varchar(12);

UPDATE webadmin.leagues
SET color_key = CASE (id - 1) % 4
    WHEN 0 THEN 'coral'
    WHEN 1 THEN 'teal'
    WHEN 2 THEN 'gold'
    ELSE 'violet'
END
WHERE color_key IS NULL;

ALTER TABLE webadmin.leagues
    ALTER COLUMN color_key SET DEFAULT 'coral',
    ALTER COLUMN color_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'leagues_color_key_allowed'
          AND conrelid = 'webadmin.leagues'::regclass
    ) THEN
        ALTER TABLE webadmin.leagues ADD CONSTRAINT leagues_color_key_allowed
            CHECK (color_key IN ('coral', 'teal', 'gold', 'violet'));
    END IF;
END $$;
