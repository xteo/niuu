-- Session icons (LEX-2629). A session registers one square icon; Forge re-encodes it to
-- PNG renditions (512, 256 and 128 px) kept here, one row per size, next to the session
-- row. sessions.icon_etag (the 512 px PNG's sha256, NULL when none) rides on every
-- session record so clients refetch only on change. Icons survive restart and archive
-- and go with the session's delete.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS icon_etag TEXT NULL;

CREATE TABLE IF NOT EXISTS session_icons (
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    size INTEGER NOT NULL CHECK (size > 0),
    etag TEXT NOT NULL,
    png BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (session_id, size)
);
