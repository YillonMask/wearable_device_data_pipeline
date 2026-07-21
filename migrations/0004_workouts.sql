CREATE TABLE workouts (
    device           TEXT NOT NULL,      -- 'whoop' | 'google_health'
    provider_id      TEXT NOT NULL,      -- the device's own workout id
    sport            TEXT,               -- labelled sport (run, strength, …)
    start_time       TEXT NOT NULL,      -- ISO-8601 UTC
    end_time         TEXT,               -- ISO-8601 UTC
    duration_minutes INTEGER,
    avg_hr           REAL,
    max_hr           REAL,
    calories         INTEGER,            -- each device's own active-cal definition
    date             TEXT NOT NULL,      -- local-tz date of start_time (for filtering)
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (device, provider_id)
);
CREATE INDEX idx_workouts_date ON workouts (date);
