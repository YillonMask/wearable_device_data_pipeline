-- Widen the subjective readiness scale from 1-10 to 1-100. SQLite can't drop
-- a CHECK constraint in place, so we recreate the table. Existing rows are
-- scaled ×10 to preserve their meaning on the new scale (e.g. an old "6"
-- becomes "60"). No foreign keys reference self_report, so no FK juggling.
CREATE TABLE self_report_new (
    date       TEXT PRIMARY KEY,
    readiness  INTEGER NOT NULL CHECK (readiness BETWEEN 1 AND 100),
    logged_at  TEXT NOT NULL
);

INSERT INTO self_report_new (date, readiness, logged_at)
SELECT date, readiness * 10, logged_at FROM self_report;

DROP TABLE self_report;
ALTER TABLE self_report_new RENAME TO self_report;
