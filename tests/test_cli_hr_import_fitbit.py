from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from wearable_pipeline import cli, db
from wearable_pipeline.clients.google_health import BASE_URL, TYPE_HEART_RATE, _list_path
from wearable_pipeline.storage import (
    create_hr_session,
    end_hr_session,
    insert_hr_samples,
    load_hr_session,
)

runner = CliRunner()

TOKEN_URL = "https://oauth2.googleapis.com/token"
HR_URL = f"{BASE_URL}{_list_path(TYPE_HEART_RATE)}"

START = datetime(2026, 8, 11, 4, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=28)


def _fitbit_point(offset_s: int, bpm: int) -> dict:
    ts = START + timedelta(seconds=offset_s)
    return {
        "dataSource": {"platform": "FITBIT"},
        "heartRate": {
            "beatsPerMinute": str(bpm),
            "sampleTime": {"physicalTime": ts.strftime("%Y-%m-%dT%H:%M:%SZ")},
        },
    }


def _mock_api(points=None) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    )
    if points is None:
        # ~5 s cadence, which is what Fitbit actually delivers
        points = [_fitbit_point(i * 5, 120 + i) for i in range(5)]
    respx.get(HR_URL).mock(return_value=httpx.Response(200, json={"dataPoints": points}))


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_HEALTH_REFRESH_TOKEN", "test-refresh")
    from wearable_pipeline import config

    monkeypatch.setattr(cli, "load_settings", config.load_settings)
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    return conn


def _seed_ble_session(conn) -> None:
    create_hr_session(
        conn,
        session_id="S1",
        label="swim",
        started_at=START.isoformat(),
        devices=["strap", "google_health"],
    )
    rows = [
        ("S1", "strap", (START + timedelta(seconds=s)).isoformat(), s * 1000, 140)
        for s in range(0, 60, 2)
    ]
    insert_hr_samples(conn, rows)
    end_hr_session(conn, session_id="S1", ended_at=END.isoformat())


@respx.mock
def test_session_merge_lands_as_fitbit_api(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_ble_session(conn)
    conn.close()
    _mock_api()

    result = runner.invoke(cli.app, ["hr-import-fitbit", "--session", "S1"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "S1")
    # merged beside the live BLE google_health series, not over it
    assert session["devices"] == ["strap", "google_health", "fitbit_api"]
    api = [s for s in samples if s["device"] == "fitbit_api"]
    assert [s["bpm"] for s in api] == [120, 121, 122, 123, 124]
    assert [s["t_offset_ms"] for s in api] == [0, 5000, 10000, 15000, 20000]
    assert "spacing=5.0s" in result.output
    conn.close()


@respx.mock
def test_session_merge_is_idempotent(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_ble_session(conn)
    conn.close()
    _mock_api()

    runner.invoke(cli.app, ["hr-import-fitbit", "--session", "S1"])
    runner.invoke(cli.app, ["hr-import-fitbit", "--session", "S1"])

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "S1")
    assert len([s for s in samples if s["device"] == "fitbit_api"]) == 5
    assert session["devices"].count("fitbit_api") == 1
    conn.close()


@respx.mock
def test_empty_window_reports_and_exits_nonzero(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_ble_session(conn)
    conn.close()
    _mock_api(points=[])

    result = runner.invoke(cli.app, ["hr-import-fitbit", "--session", "S1"])
    assert result.exit_code == 1
    assert "No Fitbit samples returned" in result.output

    # raw payload archived regardless — evidence the window really was empty
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute(
        "SELECT COUNT(*) c FROM raw_payloads WHERE device='google_health'"
    ).fetchone()["c"] == 1
    conn.close()


def test_missing_google_credentials_exits_2(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_REFRESH_TOKEN", raising=False)
    from wearable_pipeline import config

    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    result = runner.invoke(cli.app, ["hr-import-fitbit", "--session", "S1"])
    assert result.exit_code == 2
    assert "wearable auth google" in result.output


def test_requires_exactly_one_of_date_or_session(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path).close()
    assert runner.invoke(cli.app, ["hr-import-fitbit"]).exit_code == 2
    both = runner.invoke(
        cli.app, ["hr-import-fitbit", "--date", "2026-08-11", "--session", "S1"]
    )
    assert both.exit_code == 2


def test_no_stored_workouts_points_at_pull(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path).close()
    result = runner.invoke(cli.app, ["hr-import-fitbit", "--date", "2026-08-11"])
    assert result.exit_code == 1
    assert "wearable pull --date 2026-08-11" in result.output
