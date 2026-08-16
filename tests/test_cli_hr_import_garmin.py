from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from wearable_pipeline import cli, db
from wearable_pipeline.clients.garmin import GarminClient
from wearable_pipeline.storage import (
    create_hr_session,
    end_hr_session,
    insert_hr_samples,
    load_hr_session,
)
from tests.test_garmin_client import FakeApi, _details

runner = CliRunner()

# BLE session: 2026-08-11 04:10:00Z .. 04:38:00Z (the Aug 10 9:10pm PT swim).
START = datetime(2026, 8, 11, 4, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=28)


def _activity(activity_id=555, start="2026-08-11 04:10:00", duration=1680.0, name="Pool Swim"):
    return {
        "activityId": activity_id,
        "activityName": name,
        "startTimeGMT": start,
        "duration": duration,
    }


def _setup(monkeypatch, tmp_path, api: FakeApi):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "pw")
    from wearable_pipeline import config

    monkeypatch.setattr(cli, "load_settings", config.load_settings)
    monkeypatch.setattr(cli, "GarminClient", lambda **kw: GarminClient(api=api))
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    return conn


def _seed_ble_session(conn) -> None:
    create_hr_session(
        conn,
        session_id="S1",
        label="swim",
        started_at=START.isoformat(),
        devices=["strap", "whoop"],
    )
    # BLE dies after 100 s (underwater), like the real captures.
    rows = []
    for sec in range(0, 100, 2):
        ts = (START + timedelta(seconds=sec)).isoformat()
        rows.append(("S1", "strap", ts, sec * 1000, 140))
    insert_hr_samples(conn, rows)
    end_hr_session(conn, session_id="S1", ended_at=END.isoformat())


def test_session_merge_matches_activity_by_overlap(monkeypatch, tmp_path):
    api = FakeApi(details=_details(), activities=[_activity()])
    conn = _setup(monkeypatch, tmp_path, api)
    _seed_ble_session(conn)
    conn.close()

    result = runner.invoke(cli.app, ["hr-import-garmin", "--session", "S1"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "S1")
    assert session["devices"] == ["strap", "whoop", "garmin"]
    garmin = [s for s in samples if s["device"] == "garmin"]
    assert [s["bpm"] for s in garmin] == [120, 121, 122, 123, 124]
    assert [s["t_offset_ms"] for s in garmin] == [0, 1000, 2000, 3000, 4000]
    # strap untouched
    assert len([s for s in samples if s["device"] == "strap"]) == 50
    conn.close()


def test_session_merge_picks_the_most_overlapping_activity(monkeypatch, tmp_path):
    # A short unrelated activity earlier the same day, plus the real swim.
    others = [
        _activity(activity_id=111, start="2026-08-11 01:00:00", duration=600.0, name="Walk"),
        _activity(activity_id=555),
    ]
    api = FakeApi(details=_details(), activities=others)
    conn = _setup(monkeypatch, tmp_path, api)
    _seed_ble_session(conn)
    conn.close()

    result = runner.invoke(cli.app, ["hr-import-garmin", "--session", "S1"])
    assert result.exit_code == 0, result.output
    assert "activity 555" in result.output
    assert api.detail_calls[0][0] == "555"


def test_session_merge_widens_the_date_window_by_a_day(monkeypatch, tmp_path):
    """Garmin lists by local date, so a UTC-evening session must look ±1 day."""
    api = FakeApi(details=_details(), activities=[_activity()])
    conn = _setup(monkeypatch, tmp_path, api)
    _seed_ble_session(conn)
    conn.close()

    runner.invoke(cli.app, ["hr-import-garmin", "--session", "S1"])

    assert api.date_calls == [("2026-08-10", "2026-08-12")]


def test_session_merge_no_overlapping_activity(monkeypatch, tmp_path):
    api = FakeApi(
        details=_details(),
        activities=[_activity(start="2026-08-11 20:00:00", duration=600.0)],
    )
    conn = _setup(monkeypatch, tmp_path, api)
    _seed_ble_session(conn)
    conn.close()

    result = runner.invoke(cli.app, ["hr-import-garmin", "--session", "S1"])
    assert result.exit_code == 1
    assert "No Garmin activity overlaps" in result.output


def test_hand_entered_activity_reports_no_hr_channel(monkeypatch, tmp_path):
    no_hr = {
        "metricDescriptors": [{"key": "directTimestamp", "metricsIndex": 0}],
        "activityDetailMetrics": [{"metrics": [1]}],
    }
    api = FakeApi(details=no_hr, activities=[_activity()])
    conn = _setup(monkeypatch, tmp_path, api)
    _seed_ble_session(conn)
    conn.close()

    result = runner.invoke(cli.app, ["hr-import-garmin", "--session", "S1"])
    assert result.exit_code == 1
    assert "no heart-rate channel" in result.output

    # raw payload still archived — evidence about why it was empty
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute(
        "SELECT COUNT(*) c FROM raw_payloads WHERE device='garmin'"
    ).fetchone()["c"] == 1
    conn.close()


def test_session_merge_is_idempotent(monkeypatch, tmp_path):
    api = FakeApi(details=_details(), activities=[_activity()])
    conn = _setup(monkeypatch, tmp_path, api)
    _seed_ble_session(conn)
    conn.close()

    runner.invoke(cli.app, ["hr-import-garmin", "--session", "S1"])
    runner.invoke(cli.app, ["hr-import-garmin", "--session", "S1"])

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "S1")
    assert len([s for s in samples if s["device"] == "garmin"]) == 5
    assert session["devices"].count("garmin") == 1
    conn.close()


def test_date_mode_creates_its_own_session(monkeypatch, tmp_path):
    api = FakeApi(details=_details(), activities=[_activity()])
    _setup(monkeypatch, tmp_path, api).close()

    result = runner.invoke(cli.app, ["hr-import-garmin", "--date", "2026-08-11"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "20260811T041000Z-garmin")
    assert session["label"] == "Pool Swim"
    assert session["devices"] == ["garmin"]
    assert [s["bpm"] for s in samples] == [120, 121, 122, 123, 124]
    conn.close()


def test_date_mode_filters_by_activity_id(monkeypatch, tmp_path):
    api = FakeApi(
        details=_details(),
        activities=[_activity(activity_id=111, name="Walk"), _activity(activity_id=555)],
    )
    _setup(monkeypatch, tmp_path, api).close()

    result = runner.invoke(
        cli.app, ["hr-import-garmin", "--date", "2026-08-11", "--activity", "555"]
    )
    assert result.exit_code == 0, result.output
    assert "imported 1 session" in result.output
    assert api.detail_calls[0][0] == "555"


def test_date_mode_no_activities(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, FakeApi(activities=[])).close()
    result = runner.invoke(cli.app, ["hr-import-garmin", "--date", "2026-08-11"])
    assert result.exit_code == 1
    assert "No Garmin activities" in result.output


def test_requires_exactly_one_of_date_or_session(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, FakeApi()).close()
    assert runner.invoke(cli.app, ["hr-import-garmin"]).exit_code == 2
    both = runner.invoke(
        cli.app, ["hr-import-garmin", "--date", "2026-08-11", "--session", "S1"]
    )
    assert both.exit_code == 2
