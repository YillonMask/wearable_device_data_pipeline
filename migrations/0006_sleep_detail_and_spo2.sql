-- Sleep detail + blood oxygen, first used as inputs to the personal recovery
-- model. Whoop exposes all four; other devices leave them NULL until mapped.
ALTER TABLE daily_metrics ADD COLUMN spo2_pct REAL;              -- 0..100, sleep-time average
ALTER TABLE daily_metrics ADD COLUMN sleep_consistency REAL;     -- 0..1, regularity of sleep timing
ALTER TABLE daily_metrics ADD COLUMN sleep_disturbances INTEGER; -- count of wake-ups/disturbances
ALTER TABLE daily_metrics ADD COLUMN sleep_debt_minutes INTEGER; -- extra sleep needed from accumulated debt
