-- Initial schema for the wearable health data pipeline.
-- Designed to port cleanly from SQLite to Postgres later:
--   * dates stored as ISO-8601 TEXT (YYYY-MM-DD)
--   * timestamps stored as ISO-8601 TEXT (UTC)
--   * raw API responses stored as TEXT (JSON); switch to JSONB on Postgres.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- One normalized row per (date, device). Score fields use each device's
-- proprietary scale and are NOT comparable across devices — for cross-device
-- comparison use rank correlation on the raw values, not averages.
CREATE TABLE IF NOT EXISTS daily_metrics (
    date                       TEXT NOT NULL,   -- ISO date the metrics describe
    device                     TEXT NOT NULL,   -- 'oura' | 'whoop' | 'google_health'

    -- sleep
    total_sleep_minutes        INTEGER,
    sleep_efficiency           REAL,            -- 0..1
    sleep_latency_minutes      INTEGER,
    rem_minutes                INTEGER,
    deep_minutes               INTEGER,
    light_minutes              INTEGER,
    awake_minutes              INTEGER,
    sleep_score                INTEGER,         -- 0..100 (device-specific scale)

    -- recovery / readiness
    readiness_score            INTEGER,         -- 0..100 (device-specific scale)
    hrv_ms                     REAL,
    resting_hr                 REAL,
    respiratory_rate           REAL,
    body_temp_deviation        REAL,            -- Oura
    skin_temp                  REAL,            -- Whoop

    -- load
    strain_or_activity_score   REAL,
    active_calories            INTEGER,
    steps                      INTEGER,

    fetched_at                 TEXT NOT NULL,
    PRIMARY KEY (date, device)
);

CREATE INDEX IF NOT EXISTS idx_daily_metrics_device_date
    ON daily_metrics (device, date);

-- Every API response is archived here BEFORE normalization, so source data is
-- never lost if our mapping into daily_metrics turns out to be wrong.
CREATE TABLE IF NOT EXISTS raw_payloads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device     TEXT NOT NULL,
    endpoint   TEXT NOT NULL,
    date       TEXT,                            -- ISO date the payload describes (nullable for spans)
    payload    TEXT NOT NULL,                   -- raw JSON
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_payloads_device_date
    ON raw_payloads (device, date);
