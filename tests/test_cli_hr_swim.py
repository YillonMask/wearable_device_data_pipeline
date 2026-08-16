from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from wearable_pipeline import cli, db
from wearable_pipeline.clients.garmin import GarminClient
from wearable_pipeline.clients.google_health import BASE_URL, TYPE_HEART_RATE, _list_path
from wearable_pipeline.clients.whoop_internal import (
    BASE_URL as WHOOP_BASE,
    METRICS_ENDPOINT,
    SIGN_IN_ENDPOINT,
)
from wearable_pipeline.storage import load_hr_session
from tests.test_garmin_client import FakeApi

runner = CliRunner()

START = datetime(2026, 8, 11, 4, 10, 0, tzinfo=timezone.utc)

WHOOP_SIGN_IN = f"{WHOOP_BASE}{SIGN_IN_ENDPOINT}"
WHOOP_METRICS = f"{WHOOP_BASE}{METRICS_ENDPOINT}/42"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
FITBIT_HR = f"{BASE_URL}{_list_path(TYPE_HEART_RATE)}"


def _garmin_details(n: int = 10) -> dict:
    base_ms = int(START.timestamp() * 1000)
    return {
        "metricDescriptors": [
            {"key": "directTimestamp", "metricsIndex": 0},
            {"key": "directHeartRate", "metricsIndex": 1},
        ],
        "activityDetailMetrics": [
            {"metrics": [base_ms + i * 1000, 120 + i]} for i in range(n)
        ],
    }


def _activity() -> dict:
    return {
        "activityId": 555,
        "activityName": "Pool Swim",
        "startTimeGMT": "2026-08-11 04:10:00",
        "duration": 1736.0,
    }


def _setup(monkeypatch, tmp_path, *, whoop=True, google=True):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "pw")
    for key, on, value in (
        ("WHOOP_USERNAME", whoop, "me@example.com"),
        ("WHOOP_PASSWORD", whoop, "pw"),
        ("GOOGLE_HEALTH_CLIENT_ID", google, "cid"),
        ("GOOGLE_HEALTH_CLIENT_SECRET", google, "csec"),
        ("GOOGLE_HEALTH_REFRESH_TOKEN", google, "rt"),
    ):
        if on:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)

    from wearable_pipeline import config

    monkeypatch.setattr(cli, "load_settings", config.load_settings)
    api = FakeApi(details=_garmin_details(), activities=[_activity()])
    monkeypatch.setattr(cli, "GarminClient", lambda **kw: GarminClient(api=api))
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    conn.close()
    return api


def _mock_whoop(n: int = 5) -> None:
    base_ms = int(START.timestamp() * 1000)
    respx.post(WHOOP_SIGN_IN).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "user": {"id": 42}})
    )
    respx.get(WHOOP_METRICS).mock(
        return_value=httpx.Response(
            200,
            json={"values": [{"time": base_ms + i * 6000, "data": 110 + i} for i in range(n)]},
        )
    )


def _mock_fitbit(n: int = 3) -> None:
    respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    )
    points = [
        {
            "dataSource": {"platform": "FITBIT"},
            "heartRate": {
                "beatsPerMinute": str(100 + i),
                "sampleTime": {
                    "physicalTime": (START + timedelta(seconds=i * 2)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                },
            },
        }
        for i in range(n)
    ]
    respx.get(FITBIT_HR).mock(return_value=httpx.Response(200, json={"dataPoints": points}))


@respx.mock
def test_hr_swim_builds_api_only_session_from_all_three(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mock_whoop()
    _mock_fitbit()

    result = runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "20260811T041000Z-garmin")
    devices = {s["device"] for s in samples}
    # API sources only — no BLE device keys anywhere
    assert devices == {"garmin", "whoop_api", "fitbit_api"}
    assert not devices & {"strap", "whoop", "google_health"}
    assert session["devices"] == ["garmin", "whoop_api", "fitbit_api"]
    assert "whoop: +5 samples" in result.output
    assert "fitbit: +3 samples" in result.output
    conn.close()


@respx.mock
def test_hr_swim_survives_one_source_failing(monkeypatch, tmp_path):
    """A bad credential on one device must not cost the whole swim."""
    _setup(monkeypatch, tmp_path)
    respx.post(WHOOP_SIGN_IN).mock(return_value=httpx.Response(401, json={"e": "bad"}))
    _mock_fitbit()

    result = runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    assert result.exit_code == 0, result.output
    assert "whoop: FAIL" in result.output

    conn = db.connect(tmp_path / "t.db")
    _, samples = load_hr_session(conn, "20260811T041000Z-garmin")
    devices = {s["device"] for s in samples}
    assert devices == {"garmin", "fitbit_api"}  # garmin + fitbit still landed
    conn.close()


@respx.mock
def test_hr_swim_skips_unconfigured_sources(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, whoop=False, google=False)

    result = runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    assert result.exit_code == 0, result.output
    assert "whoop: SKIP" in result.output
    assert "fitbit: SKIP" in result.output

    conn = db.connect(tmp_path / "t.db")
    _, samples = load_hr_session(conn, "20260811T041000Z-garmin")
    assert {s["device"] for s in samples} == {"garmin"}
    conn.close()


@respx.mock
def test_hr_swim_empty_window_is_reported_not_fatal(monkeypatch, tmp_path):
    """Fitbit records nothing while swimming — that must read as empty, not error."""
    _setup(monkeypatch, tmp_path)
    _mock_whoop()
    _mock_fitbit(n=0)

    result = runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    assert result.exit_code == 0, result.output
    assert "fitbit: no samples in this window" in result.output

    # raw still archived: an empty window is itself evidence
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute(
        "SELECT COUNT(*) c FROM raw_payloads WHERE device='google_health'"
    ).fetchone()["c"] == 1
    conn.close()


@respx.mock
def test_hr_swim_is_idempotent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mock_whoop()
    _mock_fitbit()

    runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "20260811T041000Z-garmin")
    assert len([s for s in samples if s["device"] == "whoop_api"]) == 5
    assert len([s for s in samples if s["device"] == "garmin"]) == 10
    assert session["devices"].count("fitbit_api") == 1
    conn.close()


@respx.mock
def test_reported_avg_describes_only_the_imported_series(monkeypatch, tmp_path):
    """Re-running must not fold other devices' bpm into the Garmin average."""
    _setup(monkeypatch, tmp_path)
    _mock_whoop()
    _mock_fitbit()

    first = runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    # Garmin-only average of 120..129
    assert "avg=124.5" in first.output

    # Second run: whoop (110..114) and fitbit (100..102) are already in the
    # session, but the Garmin line must be unchanged.
    second = runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    assert "avg=124.5" in second.output, second.output


def test_hr_swim_no_garmin_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    from wearable_pipeline import config

    monkeypatch.setattr(cli, "load_settings", config.load_settings)
    monkeypatch.setattr(
        cli, "GarminClient", lambda **kw: GarminClient(api=FakeApi(activities=[]))
    )
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    conn.close()

    result = runner.invoke(cli.app, ["hr-swim", "--date", "2026-08-11"])
    assert result.exit_code == 1
    assert "No Garmin activities" in result.output
