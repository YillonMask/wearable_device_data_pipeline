from __future__ import annotations

from wearable_pipeline.storage import (
    create_hr_session,
    end_hr_session,
    insert_hr_samples,
    load_hr_session,
)


def test_session_roundtrip(migrated_db):
    conn = migrated_db
    create_hr_session(
        conn,
        session_id="20260721T183005Z",
        label="bike",
        started_at="2026-07-21T18:30:05+00:00",
        devices=["whoop", "google_health"],
    )
    insert_hr_samples(
        conn,
        [
            ("20260721T183005Z", "whoop", "2026-07-21T18:30:06+00:00", 1000, 120),
            ("20260721T183005Z", "google_health", "2026-07-21T18:30:06+00:00", 1000, 118),
        ],
    )
    end_hr_session(conn, session_id="20260721T183005Z", ended_at="2026-07-21T19:00:00+00:00")

    session, samples = load_hr_session(conn, "20260721T183005Z")
    assert session["label"] == "bike"
    assert session["ended_at"] == "2026-07-21T19:00:00+00:00"
    assert session["devices"] == ["whoop", "google_health"]
    assert len(samples) == 2
    assert {s["device"] for s in samples} == {"whoop", "google_health"}
    assert samples[0]["bpm"] in (118, 120)


def test_load_missing_session_raises(migrated_db):
    import pytest

    with pytest.raises(KeyError):
        load_hr_session(migrated_db, "nope")
