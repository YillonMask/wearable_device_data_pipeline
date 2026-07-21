from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from wearable_pipeline.clients.whoop import (
    BASE_URL,
    CYCLE_ENDPOINT,
    RECOVERY_ENDPOINT,
    SLEEP_ENDPOINT,
    WhoopClient,
    _end_local_date,
    _pick_main_sleep,
)

WORKOUT_ENDPOINT_PATH = "v2/activity/workout"

TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("WHOOP_REFRESH_TOKEN=test-refresh\n")
    return p


def _make_client(env_file: Path) -> WhoopClient:
    return WhoopClient(
        client_id="cid",
        client_secret="csec",
        refresh_token="test-refresh",
        env_path=env_file,
        local_timezone="America/Los_Angeles",
    )


# --- pure helpers ------------------------------------------------------------


def test_end_local_date_z_suffix_converts_to_local() -> None:
    """A sleep ending 06:30 UTC Tue is 23:30 PDT Mon, so day = Mon."""
    tz = ZoneInfo("America/Los_Angeles")
    assert _end_local_date({"end": "2026-06-23T06:30:00Z"}, tz) == date(2026, 6, 22)


def test_end_local_date_with_offset() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    record = {"end": "2026-06-12T07:30:00-07:00"}
    assert _end_local_date(record, tz) == date(2026, 6, 12)


def test_pick_main_sleep_skips_naps_and_off_day_records() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    target = date(2026, 6, 12)
    records = [
        {"id": 1, "nap": True, "end": "2026-06-12T15:30:00-07:00"},   # nap
        {"id": 2, "nap": False, "end": "2026-06-11T08:00:00-07:00"},  # prior day
        {"id": 3, "nap": False, "end": "2026-06-12T07:30:00-07:00"},  # ✓ target
        {"id": 4, "nap": False, "end": "2026-06-13T07:30:00-07:00"},  # next day
    ]
    assert _pick_main_sleep(records, target, tz)["id"] == 3


def test_pick_main_sleep_returns_none_when_only_naps() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    records = [{"id": 1, "nap": True, "end": "2026-06-12T15:00:00-07:00"}]
    assert _pick_main_sleep(records, date(2026, 6, 12), tz) is None


# --- fetch_day with mocked HTTP ---------------------------------------------


def _stub_token() -> None:
    respx.post(TOKEN_URL).respond(
        200, json={"access_token": "access-1", "expires_in": 3600}
    )


@respx.mock
def test_fetch_day_normalizes_full_record(env_file: Path) -> None:
    day = date(2026, 6, 12)
    _stub_token()
    respx.get(BASE_URL + CYCLE_ENDPOINT).respond(
        200,
        json={
            "records": [
                {
                    "id": 9991,
                    "start": "2026-06-12T07:30:00-07:00",
                    "end": "2026-06-13T07:00:00-07:00",
                    "timezone_offset": "-07:00",
                    "score_state": "SCORED",
                    "score": {
                        "strain": 14.5,
                        "kilojoule": 7000,
                        "average_heart_rate": 70,
                        "max_heart_rate": 145,
                    },
                }
            ],
            "next_token": None,
        },
    )
    respx.get(BASE_URL + RECOVERY_ENDPOINT).respond(
        200,
        json={
            "records": [
                {
                    "cycle_id": 9991,
                    "sleep_id": 5551,
                    "score": {
                        "recovery_score": 75,
                        "resting_heart_rate": 50,
                        "hrv_rmssd_milli": 45.5,
                        "skin_temp_celsius": 35.5,
                    },
                }
            ],
            "next_token": None,
        },
    )
    respx.get(BASE_URL + SLEEP_ENDPOINT).respond(
        200,
        json={
            "records": [
                {
                    "id": 5551,
                    "cycle_id": 9991,
                    "start": "2026-06-11T22:30:00-07:00",
                    "end": "2026-06-12T07:00:00-07:00",
                    "nap": False,
                    "score": {
                        "stage_summary": {
                            "total_in_bed_time_milli": 30600000,
                            "total_awake_time_milli": 1800000,
                            "total_light_sleep_time_milli": 14400000,
                            "total_slow_wave_sleep_time_milli": 7200000,
                            "total_rem_sleep_time_milli": 7200000,
                        },
                        "respiratory_rate": 14.5,
                        "sleep_performance_percentage": 91.0,
                        "sleep_efficiency_percentage": 89.0,
                    },
                }
            ],
            "next_token": None,
        },
    )

    result = _make_client(env_file).fetch_day(day)
    m = result.metrics
    assert m.device == "whoop"
    assert m.date == day
    # total in bed (30600s) - awake (1800s) = 28800s = 480 min
    assert m.total_sleep_minutes == 480
    assert m.sleep_efficiency == pytest.approx(0.89)
    assert m.rem_minutes == 120
    assert m.deep_minutes == 120
    assert m.light_minutes == 240
    assert m.awake_minutes == 30
    assert m.sleep_score == 91
    assert m.readiness_score == 75
    assert m.hrv_ms == 45.5
    assert m.resting_hr == 50
    assert m.respiratory_rate == 14.5
    assert m.skin_temp == 35.5
    assert m.strain_or_activity_score == 14.5
    # 7000 kJ / 4.184 ≈ 1673 kcal
    assert m.active_calories == 1673
    assert m.steps is None
    assert len(result.raw) == 3


@respx.mock
def test_fetch_day_with_only_a_nap_returns_empty_metrics(env_file: Path) -> None:
    day = date(2026, 6, 12)
    _stub_token()
    respx.get(BASE_URL + CYCLE_ENDPOINT).respond(
        200, json={"records": [], "next_token": None}
    )
    respx.get(BASE_URL + RECOVERY_ENDPOINT).respond(
        200, json={"records": [], "next_token": None}
    )
    respx.get(BASE_URL + SLEEP_ENDPOINT).respond(
        200,
        json={
            "records": [
                {"id": 1, "nap": True, "end": "2026-06-12T15:00:00-07:00"}
            ],
            "next_token": None,
        },
    )

    result = _make_client(env_file).fetch_day(day)
    m = result.metrics
    assert m.total_sleep_minutes is None
    assert m.readiness_score is None
    assert m.strain_or_activity_score is None
    # Raw payloads still archived even when normalization is empty.
    assert len(result.raw) == 3


@respx.mock
def test_fetch_day_refreshes_on_401(env_file: Path) -> None:
    day = date(2026, 6, 12)
    token_route = respx.post(TOKEN_URL).respond(
        200, json={"access_token": "access-1", "expires_in": 3600}
    )
    # First cycle request returns 401, second returns 200.
    cycle_route = respx.get(BASE_URL + CYCLE_ENDPOINT).mock(
        side_effect=[
            httpx.Response(401, json={"detail": "expired"}),
            httpx.Response(200, json={"records": [], "next_token": None}),
        ]
    )
    respx.get(BASE_URL + RECOVERY_ENDPOINT).respond(
        200, json={"records": [], "next_token": None}
    )
    respx.get(BASE_URL + SLEEP_ENDPOINT).respond(
        200, json={"records": [], "next_token": None}
    )

    _make_client(env_file).fetch_day(day)

    # One initial refresh + one forced refresh on 401 = 2 token calls.
    assert token_route.call_count == 2
    assert cycle_route.call_count == 2


@respx.mock
def test_fetch_workouts_maps_fields(env_file: Path) -> None:
    """fetch_workouts returns normalized Workout objects with correct field mapping."""
    _stub_token()

    body = {
        "records": [
            {
                "id": "w1",
                "start": "2026-06-24T20:00:00.000Z",
                "end": "2026-06-24T21:00:00.000Z",
                "sport_name": "running",
                "score": {
                    "average_heart_rate": 145,
                    "max_heart_rate": 178,
                    "kilojoule": 2000.0,
                },
            }
        ],
        "next_token": None,
    }
    respx.get(BASE_URL + WORKOUT_ENDPOINT_PATH).respond(200, json=body)

    client = _make_client(env_file)
    result = client.fetch_workouts(date(2026, 6, 24), date(2026, 6, 24))

    assert len(result.workouts) == 1
    w = result.workouts[0]
    assert w.device == "whoop"
    assert w.provider_id == "w1"
    assert w.sport == "running"
    assert w.avg_hr == 145
    assert w.max_hr == 178
    assert w.calories == round(2000.0 / 4.184)  # 478
    assert w.duration_minutes == 60
    # start_time is 2026-06-24T20:00:00Z → America/Los_Angeles (PDT = UTC-7) → 13:00 → date 2026-06-24
    assert w.date.isoformat() == "2026-06-24"
    assert any("workout" in r.endpoint for r in result.raw)
