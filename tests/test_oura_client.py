from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from wearable_pipeline.clients.oura import (
    BASE_URL,
    DAILY_ACTIVITY,
    DAILY_READINESS,
    DAILY_SLEEP,
    SLEEP_SESSIONS,
    OuraClient,
)

FIXTURES = Path(__file__).parent / "fixtures" / "oura"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _full_url(path: str) -> str:
    return BASE_URL + path


@pytest.fixture
def oura_client() -> OuraClient:
    return OuraClient(personal_access_token="test-token-not-real")


@respx.mock
def test_fetch_day_golden_path(oura_client: OuraClient) -> None:
    day = date(2026, 6, 21)
    respx.get(_full_url(DAILY_SLEEP)).respond(200, json=_load("daily_sleep.json"))
    respx.get(_full_url(DAILY_READINESS)).respond(
        200, json=_load("daily_readiness.json")
    )
    respx.get(_full_url(DAILY_ACTIVITY)).respond(
        200, json=_load("daily_activity.json")
    )
    respx.get(_full_url(SLEEP_SESSIONS)).respond(200, json=_load("sleep.json"))

    result = oura_client.fetch_day(day)

    m = result.metrics
    assert m.device == "oura"
    assert m.date == day
    assert m.sleep_score == 82
    assert m.readiness_score == 88
    assert m.body_temp_deviation == pytest.approx(-0.12)
    assert m.strain_or_activity_score == 78
    assert m.active_calories == 420
    assert m.steps == 8521
    # 25200 seconds = 420 minutes — the long sleep is picked over the nap.
    assert m.total_sleep_minutes == 420
    assert m.sleep_efficiency == pytest.approx(0.93)
    assert m.sleep_latency_minutes == 8  # 480 / 60
    assert m.rem_minutes == 100  # 6000 / 60
    assert m.deep_minutes == 90  # 5400 / 60
    assert m.light_minutes == 230  # 13800 / 60
    assert m.awake_minutes == 18  # 1080 / 60
    assert m.hrv_ms == 52
    assert m.resting_hr == 48
    assert m.respiratory_rate == pytest.approx(14.5)

    assert len(result.raw) == 4
    assert {r.endpoint for r in result.raw} == {
        DAILY_SLEEP,
        DAILY_READINESS,
        DAILY_ACTIVITY,
        SLEEP_SESSIONS,
    }
    assert all(r.date == "2026-06-21" for r in result.raw)


@respx.mock
def test_fetch_day_no_ring_data_returns_all_none(oura_client: OuraClient) -> None:
    day = date(2026, 6, 21)
    empty = {"data": [], "next_token": None}
    for path in (DAILY_SLEEP, DAILY_READINESS, DAILY_ACTIVITY, SLEEP_SESSIONS):
        respx.get(_full_url(path)).respond(200, json=empty)

    result = oura_client.fetch_day(day)

    m = result.metrics
    assert m.device == "oura"
    assert m.date == day
    # Every normalized field is None — we don't substitute zeros for "no data".
    for field in (
        "sleep_score",
        "readiness_score",
        "total_sleep_minutes",
        "sleep_efficiency",
        "steps",
        "hrv_ms",
        "resting_hr",
    ):
        assert getattr(m, field) is None, f"{field} should be None on empty data"

    # Raw payloads still written — source data preserved even when empty.
    assert len(result.raw) == 4


@respx.mock
def test_fetch_day_401_propagates(oura_client: OuraClient) -> None:
    day = date(2026, 6, 21)
    respx.get(_full_url(DAILY_SLEEP)).respond(
        401, json={"detail": "Invalid token"}
    )

    with pytest.raises(httpx.HTTPStatusError) as exc:
        oura_client.fetch_day(day)
    assert exc.value.response.status_code == 401


@respx.mock
def test_fetch_day_sends_widened_date_range(oura_client: OuraClient) -> None:
    """All endpoints query a day-1..day+1 window because Oura's date filter
    drops records when start_date == end_date."""
    day = date(2026, 6, 21)
    empty = {"data": [], "next_token": None}
    routes = {
        p: respx.get(_full_url(p)).respond(200, json=empty)
        for p in (DAILY_SLEEP, DAILY_READINESS, DAILY_ACTIVITY, SLEEP_SESSIONS)
    }

    oura_client.fetch_day(day)

    for r in routes.values():
        params = dict(r.calls.last.request.url.params)
        assert params["start_date"] == "2026-06-20"
        assert params["end_date"] == "2026-06-22"


@respx.mock
def test_response_filtered_to_target_day(oura_client: OuraClient) -> None:
    """The widened window can return records for day-1 and day+1; only those
    whose ``day`` field matches the target should populate DailyMetrics."""
    day = date(2026, 6, 21)
    target = "2026-06-21"

    def envelope(items: list[dict]) -> dict:
        return {"data": items, "next_token": None}

    respx.get(_full_url(DAILY_SLEEP)).respond(
        200,
        json=envelope(
            [
                {"day": "2026-06-20", "score": 11},
                {"day": target, "score": 77},
                {"day": "2026-06-22", "score": 99},
            ]
        ),
    )
    respx.get(_full_url(DAILY_READINESS)).respond(
        200, json=envelope([{"day": target, "score": 65}])
    )
    respx.get(_full_url(DAILY_ACTIVITY)).respond(
        200,
        json=envelope(
            [
                {"day": "2026-06-22", "score": 50, "steps": 99999},
                {"day": target, "score": 80, "steps": 8000, "active_calories": 400},
            ]
        ),
    )
    respx.get(_full_url(SLEEP_SESSIONS)).respond(
        200,
        json=envelope(
            [
                {"day": "2026-06-20", "total_sleep_duration": 30000, "efficiency": 99},
                {
                    "day": target,
                    "total_sleep_duration": 25200,
                    "efficiency": 93,
                    "average_hrv": 52,
                },
                {"day": "2026-06-22", "total_sleep_duration": 99999, "efficiency": 100},
            ]
        ),
    )

    result = oura_client.fetch_day(day)

    m = result.metrics
    # Picks only the target-day records, not the neighbors with bigger numbers.
    assert m.sleep_score == 77
    assert m.readiness_score == 65
    assert m.strain_or_activity_score == 80
    assert m.steps == 8000
    assert m.total_sleep_minutes == 420
    assert m.sleep_efficiency == pytest.approx(0.93)
    assert m.hrv_ms == 52
