CREATE TABLE hr_sessions (
    id          TEXT PRIMARY KEY,   -- compact ISO-8601 UTC start, e.g. 20260721T183005Z
    label       TEXT,               -- 'bike' | 'treadmill' | ...
    started_at  TEXT NOT NULL,      -- ISO-8601 UTC (extended)
    ended_at    TEXT,               -- ISO-8601 UTC; NULL until stopped
    devices     TEXT                -- JSON array, e.g. ["whoop","google_health"]
);

CREATE TABLE hr_samples (
    session_id  TEXT NOT NULL,      -- FK -> hr_sessions.id
    device      TEXT NOT NULL,      -- 'whoop' | 'google_health'
    ts_utc      TEXT NOT NULL,      -- receipt time, ISO-8601 UTC
    t_offset_ms INTEGER NOT NULL,   -- ms since session start
    bpm         INTEGER NOT NULL,
    PRIMARY KEY (session_id, device, ts_utc)
);

CREATE INDEX idx_hr_samples_session ON hr_samples (session_id, device);
