from __future__ import annotations

from pathlib import Path

from wearable_pipeline import db


def test_migrate_creates_expected_tables(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "test.db")
    applied = db.migrate(conn)
    assert "0001_init" in applied

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"daily_metrics", "raw_payloads", "schema_migrations"} <= tables


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    assert db.migrate(conn) == []


def test_migration_0002_creates_self_report_and_manual_metrics(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "self_report" in tables
    assert "manual_metrics" in tables

    self_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(self_report)")
    }
    assert self_cols == {"date", "readiness", "logged_at"}

    manual_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(manual_metrics)")
    }
    assert manual_cols == {"date", "device", "readiness_score", "logged_at"}

    import sqlite3 as _sq

    import pytest as _pt

    with _pt.raises(_sq.IntegrityError):
        conn.execute(
            "INSERT INTO self_report (date, readiness, logged_at) "
            "VALUES ('2026-06-22', 101, '2026-06-22T08:00:00+00:00')"
        )
    with _pt.raises(_sq.IntegrityError):
        conn.execute(
            "INSERT INTO manual_metrics (date, device, readiness_score, logged_at) "
            "VALUES ('2026-06-22', 'google_health', 101, '2026-06-22T08:00:00+00:00')"
        )

    conn.close()


def test_migrate_creates_workouts_table(tmp_path):
    import wearable_pipeline.db as db
    conn = db.connect(tmp_path / "w.db")
    db.migrate(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workouts)").fetchall()}
    assert cols == {
        "device", "provider_id", "sport", "start_time", "end_time",
        "duration_minutes", "avg_hr", "max_hr", "calories", "date", "fetched_at",
    }
    pk = [row[1] for row in conn.execute("PRAGMA table_info(workouts)").fetchall() if row[5]]
    assert set(pk) == {"device", "provider_id"}
