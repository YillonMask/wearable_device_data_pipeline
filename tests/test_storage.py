from __future__ import annotations

from datetime import date

from wearable_pipeline.models import DailyMetrics
from wearable_pipeline.storage import upsert_daily_metrics, write_raw_payload


def test_upsert_daily_metrics_round_trip(migrated_db) -> None:
    upsert_daily_metrics(
        migrated_db,
        DailyMetrics(
            date=date(2026, 1, 15),
            device="oura",
            total_sleep_minutes=420,
            readiness_score=82,
        ),
    )

    row = migrated_db.execute(
        "SELECT date, device, total_sleep_minutes, readiness_score "
        "FROM daily_metrics WHERE device = 'oura'"
    ).fetchone()
    assert row["date"] == "2026-01-15"
    assert row["total_sleep_minutes"] == 420
    assert row["readiness_score"] == 82


def test_upsert_daily_metrics_overwrites_existing(migrated_db) -> None:
    upsert_daily_metrics(
        migrated_db,
        DailyMetrics(date=date(2026, 1, 15), device="oura", readiness_score=70),
    )
    upsert_daily_metrics(
        migrated_db,
        DailyMetrics(date=date(2026, 1, 15), device="oura", readiness_score=88),
    )

    row = migrated_db.execute(
        "SELECT readiness_score FROM daily_metrics WHERE device = 'oura'"
    ).fetchone()
    assert row["readiness_score"] == 88


def test_write_raw_payload(migrated_db) -> None:
    rid = write_raw_payload(
        migrated_db,
        device="oura",
        endpoint="/v2/usercollection/daily_readiness",
        date="2026-01-15",
        payload={"data": [{"score": 82}]},
    )
    assert rid is not None

    row = migrated_db.execute(
        "SELECT device, endpoint, date FROM raw_payloads WHERE id = ?",
        (rid,),
    ).fetchone()
    assert row["device"] == "oura"
    assert row["endpoint"] == "/v2/usercollection/daily_readiness"
    assert row["date"] == "2026-01-15"


import pytest


def test_upsert_self_report_round_trip(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_self_report

    upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=7)

    row = migrated_db.execute(
        "SELECT date, readiness, logged_at FROM self_report"
    ).fetchone()
    assert row["date"] == "2026-06-22"
    assert row["readiness"] == 7
    assert row["logged_at"]


def test_upsert_self_report_overwrites_existing(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_self_report

    upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=7)
    first_logged_at = migrated_db.execute(
        "SELECT logged_at FROM self_report"
    ).fetchone()["logged_at"]

    upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=5)

    rows = migrated_db.execute(
        "SELECT readiness, logged_at FROM self_report"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["readiness"] == 5
    assert rows[0]["logged_at"] >= first_logged_at


def test_upsert_self_report_rejects_out_of_range(migrated_db) -> None:
    import sqlite3

    from wearable_pipeline.storage import upsert_self_report

    with pytest.raises(sqlite3.IntegrityError):
        upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=101)
    with pytest.raises(sqlite3.IntegrityError):
        upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=0)


def test_upsert_manual_readiness_round_trip(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_manual_readiness

    upsert_manual_readiness(
        migrated_db,
        day=date(2026, 6, 22),
        device="google_health",
        readiness_score=48,
    )

    row = migrated_db.execute(
        "SELECT date, device, readiness_score, logged_at FROM manual_metrics"
    ).fetchone()
    assert row["date"] == "2026-06-22"
    assert row["device"] == "google_health"
    assert row["readiness_score"] == 48
    assert row["logged_at"]


def test_upsert_manual_readiness_overwrites_existing(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_manual_readiness

    upsert_manual_readiness(
        migrated_db,
        day=date(2026, 6, 22),
        device="google_health",
        readiness_score=48,
    )
    upsert_manual_readiness(
        migrated_db,
        day=date(2026, 6, 22),
        device="google_health",
        readiness_score=62,
    )

    rows = migrated_db.execute(
        "SELECT readiness_score FROM manual_metrics WHERE device = 'google_health'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["readiness_score"] == 62


def test_upsert_manual_readiness_rejects_out_of_range(migrated_db) -> None:
    import sqlite3

    from wearable_pipeline.storage import upsert_manual_readiness

    with pytest.raises(sqlite3.IntegrityError):
        upsert_manual_readiness(
            migrated_db,
            day=date(2026, 6, 22),
            device="google_health",
            readiness_score=101,
        )
    with pytest.raises(sqlite3.IntegrityError):
        upsert_manual_readiness(
            migrated_db,
            day=date(2026, 6, 22),
            device="google_health",
            readiness_score=-1,
        )


from datetime import date, datetime, timezone


def test_upsert_workout_inserts_then_updates(tmp_path):
    import wearable_pipeline.db as db
    from wearable_pipeline.models import Workout
    from wearable_pipeline.storage import upsert_workout

    conn = db.connect(tmp_path / "w.db")
    db.migrate(conn)

    w = Workout(
        device="whoop", provider_id="abc", sport="running",
        start_time=datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 24, 21, 0, tzinfo=timezone.utc),
        duration_minutes=60, avg_hr=145.0, max_hr=178.0, calories=478,
        date=date(2026, 6, 24),
    )
    upsert_workout(conn, w)
    row = conn.execute(
        "SELECT sport, avg_hr, max_hr, calories FROM workouts WHERE device=? AND provider_id=?",
        ("whoop", "abc"),
    ).fetchone()
    assert tuple(row) == ("running", 145.0, 178.0, 478)

    # Re-upsert same key with changed values → update in place, still one row.
    upsert_workout(conn, w.model_copy(update={"avg_hr": 150.0, "calories": 500}))
    rows = conn.execute("SELECT avg_hr, calories FROM workouts").fetchall()
    assert [tuple(r) for r in rows] == [(150.0, 500)]
