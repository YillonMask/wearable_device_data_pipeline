-- Subjective readiness log: one 1-10 score per morning, user-entered before
-- any wearable data is viewed, to keep the rating un-anchored.
CREATE TABLE IF NOT EXISTS self_report (
    date       TEXT PRIMARY KEY,
    readiness  INTEGER NOT NULL CHECK (readiness BETWEEN 1 AND 10),
    logged_at  TEXT NOT NULL
);

-- Manually-entered readiness for Google Health (Fitbit Air): visible in the
-- Fitbit phone app but never returned by the v4 API (confirmed empirically
-- 2026-06-22 — see CLAUDE.md). Kept separate from daily_metrics so
-- daily_metrics remains strictly API-derived and the morning pull's UPSERT
-- cannot clobber a manually-entered value with NULL.
CREATE TABLE IF NOT EXISTS manual_metrics (
    date             TEXT NOT NULL,
    device           TEXT NOT NULL,
    readiness_score  INTEGER NOT NULL CHECK (readiness_score BETWEEN 0 AND 100),
    logged_at        TEXT NOT NULL,
    PRIMARY KEY (date, device)
);
