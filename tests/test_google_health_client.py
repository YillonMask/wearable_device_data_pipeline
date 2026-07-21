from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import respx

from wearable_pipeline.clients.google_health import (
    BASE_URL,
    TYPE_ACTIVE_ENERGY,
    TYPE_HRV,
    TYPE_RESPIRATORY,
    TYPE_RHR,
    TYPE_SLEEP,
    TYPE_SPO2,
    TYPE_STEPS,
    TYPE_WORKOUT,
    GoogleHealthClient,
    _date_proto_matches,
    _is_fitbit_source,
    _list_path,
    _rollup_path,
    _summarize_sleep_for_day,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
LA = ZoneInfo("America/Los_Angeles")


def _fitbit_source() -> dict:
    # Real shape observed against the live API.
    return {"recordingMethod": "DERIVED", "device": {}, "platform": "FITBIT"}


def _apple_source() -> dict:
    return {"platform": "APPLE_HEALTH"}


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("GOOGLE_HEALTH_REFRESH_TOKEN=test-refresh\n")
    return p


def _make_client(env_file: Path) -> GoogleHealthClient:
    return GoogleHealthClient(
        client_id="cid",
        client_secret="csec",
        refresh_token="test-refresh",
        env_path=env_file,
        local_timezone="America/Los_Angeles",
    )


def _stub_token() -> None:
    respx.post(TOKEN_URL).respond(
        200, json={"access_token": "access-1", "expires_in": 3600}
    )


# --- pure helpers -----------------------------------------------------------


def test_is_fitbit_source_matches_platform_fitbit() -> None:
    assert _is_fitbit_source(_fitbit_source()) is True


def test_is_fitbit_source_rejects_other_platforms() -> None:
    assert _is_fitbit_source(_apple_source()) is False


def test_is_fitbit_source_missing_data_source_is_permissive() -> None:
    # Rollup responses sometimes omit dataSource; don't drop those records.
    assert _is_fitbit_source(None) is True


def test_is_fitbit_source_legacy_manufacturer_field() -> None:
    assert _is_fitbit_source({"device": {"manufacturer": "Fitbit"}}) is True


def test_date_proto_matches() -> None:
    target = date(2026, 6, 22)
    assert _date_proto_matches({"year": 2026, "month": 6, "day": 22}, target)
    assert not _date_proto_matches({"year": 2026, "month": 6, "day": 21}, target)
    assert not _date_proto_matches(None, target)


def test_summarize_sleep_picks_main_session_ending_on_target_day() -> None:
    body = {
        "dataPoints": [
            {
                "dataSource": _fitbit_source(),
                "sleep": {
                    "interval": {
                        "startTime": "2026-06-21T22:00:00-07:00",
                        "endTime": "2026-06-22T07:00:00-07:00",
                    },
                    "summary": {
                        "minutesAsleep": "420",
                        "minutesAwake": "30",
                        "minutesInSleepPeriod": "450",
                        "minutesToFallAsleep": "8",
                        "stagesSummary": [
                            {"type": "REM", "minutes": "90"},
                            {"type": "DEEP", "minutes": "75"},
                            {"type": "LIGHT", "minutes": "240"},
                            {"type": "AWAKE", "minutes": "15"},
                        ],
                    },
                },
            },
            {
                # nap on the same day but shorter — should NOT be the main session
                "dataSource": _fitbit_source(),
                "sleep": {
                    "interval": {
                        "startTime": "2026-06-22T14:00:00-07:00",
                        "endTime": "2026-06-22T15:00:00-07:00",
                    },
                    "summary": {
                        "minutesAsleep": "45",
                        "minutesAwake": "5",
                        "minutesInSleepPeriod": "50",
                        "stagesSummary": [],
                    },
                },
            },
        ]
    }
    out = _summarize_sleep_for_day(body, date(2026, 6, 22), LA)
    assert out["total_sleep_minutes"] == 420
    assert out["sleep_efficiency"] == pytest.approx(420 / 450)
    assert out["rem_minutes"] == 90
    assert out["deep_minutes"] == 75
    assert out["light_minutes"] == 240


def test_summarize_sleep_drops_records_ending_on_other_days() -> None:
    body = {
        "dataPoints": [
            {
                "dataSource": _fitbit_source(),
                "sleep": {
                    "interval": {"endTime": "2026-06-21T07:00:00-07:00"},
                    "summary": {"minutesAsleep": "999"},
                },
            }
        ]
    }
    assert _summarize_sleep_for_day(body, date(2026, 6, 22), LA) == {}


def test_summarize_sleep_drops_non_fitbit_records() -> None:
    body = {
        "dataPoints": [
            {
                "dataSource": _apple_source(),
                "sleep": {
                    "interval": {"endTime": "2026-06-22T07:00:00-07:00"},
                    "summary": {"minutesAsleep": "999"},
                },
            }
        ]
    }
    assert _summarize_sleep_for_day(body, date(2026, 6, 22), LA) == {}


# --- fetch_day --------------------------------------------------------------


@respx.mock
def test_fetch_day_normalizes_full_record(env_file: Path) -> None:
    day = date(2026, 6, 22)
    _stub_token()

    src = _fitbit_source()

    respx.get(BASE_URL + _list_path(TYPE_HRV)).respond(
        200,
        json={
            "dataPoints": [
                {
                    "dataSource": src,
                    "dailyHeartRateVariability": {
                        "date": {"year": 2026, "month": 6, "day": 22},
                        "averageHeartRateVariabilityMilliseconds": 48.7,
                    },
                },
                {
                    # Different day — must be filtered out
                    "dataSource": src,
                    "dailyHeartRateVariability": {
                        "date": {"year": 2026, "month": 6, "day": 21},
                        "averageHeartRateVariabilityMilliseconds": 999.0,
                    },
                },
            ]
        },
    )
    respx.get(BASE_URL + _list_path(TYPE_RHR)).respond(
        200,
        json={
            "dataPoints": [
                {
                    "dataSource": src,
                    "dailyRestingHeartRate": {
                        "date": {"year": 2026, "month": 6, "day": 22},
                        "beatsPerMinute": "55",
                    },
                }
            ]
        },
    )
    respx.get(BASE_URL + _list_path(TYPE_RESPIRATORY)).respond(
        200,
        json={
            "dataPoints": [
                {
                    "dataSource": src,
                    "dailyRespiratoryRate": {
                        "date": {"year": 2026, "month": 6, "day": 22},
                        "breathsPerMinute": 15.5,
                    },
                }
            ]
        },
    )
    respx.get(BASE_URL + _list_path(TYPE_SPO2)).respond(
        200, json={"dataPoints": []}
    )
    respx.get(BASE_URL + _list_path(TYPE_SLEEP)).respond(
        200,
        json={
            "dataPoints": [
                {
                    "dataSource": src,
                    "sleep": {
                        "interval": {
                            "startTime": "2026-06-21T22:00:00-07:00",
                            "endTime": "2026-06-22T07:00:00-07:00",
                        },
                        "summary": {
                            "minutesAsleep": "420",
                            "minutesAwake": "30",
                            "minutesInSleepPeriod": "450",
                            "minutesToFallAsleep": "8",
                            "stagesSummary": [
                                {"type": "REM", "minutes": "90"},
                                {"type": "DEEP", "minutes": "75"},
                                {"type": "LIGHT", "minutes": "240"},
                                {"type": "AWAKE", "minutes": "15"},
                            ],
                        },
                    },
                }
            ]
        },
    )
    respx.post(BASE_URL + _rollup_path(TYPE_STEPS)).respond(
        200,
        json={"rollupDataPoints": [{"steps": {"countSum": "9543"}}]},
    )
    respx.post(BASE_URL + _rollup_path(TYPE_ACTIVE_ENERGY)).respond(
        200,
        json={"rollupDataPoints": [{"activeEnergyBurned": {"kcalSum": 425.7}}]},
    )

    result = _make_client(env_file).fetch_day(day)
    m = result.metrics
    assert m.device == "google_health"
    assert m.date == day
    assert m.hrv_ms == 48.7
    assert m.resting_hr == 55.0
    assert m.respiratory_rate == 15.5
    assert m.total_sleep_minutes == 420
    assert m.sleep_efficiency == pytest.approx(420 / 450)
    assert m.rem_minutes == 90
    assert m.deep_minutes == 75
    assert m.light_minutes == 240
    assert m.steps == 9543
    assert m.active_calories == 426
    assert m.sleep_score is None
    assert m.readiness_score is None
    assert len(result.raw) == 7


@respx.mock
def test_fetch_day_filters_out_non_fitbit_sources(env_file: Path) -> None:
    day = date(2026, 6, 22)
    _stub_token()

    apple_only_rhr = {
        "dataPoints": [
            {
                "dataSource": _apple_source(),
                "dailyRestingHeartRate": {
                    "date": {"year": 2026, "month": 6, "day": 22},
                    "beatsPerMinute": "60",
                },
            }
        ]
    }
    respx.get(BASE_URL + _list_path(TYPE_HRV)).respond(200, json={"dataPoints": []})
    respx.get(BASE_URL + _list_path(TYPE_RHR)).respond(200, json=apple_only_rhr)
    respx.get(BASE_URL + _list_path(TYPE_RESPIRATORY)).respond(
        200, json={"dataPoints": []}
    )
    respx.get(BASE_URL + _list_path(TYPE_SPO2)).respond(
        200, json={"dataPoints": []}
    )
    respx.get(BASE_URL + _list_path(TYPE_SLEEP)).respond(
        200, json={"dataPoints": []}
    )
    respx.post(BASE_URL + _rollup_path(TYPE_STEPS)).respond(
        200, json={"rollupDataPoints": []}
    )
    respx.post(BASE_URL + _rollup_path(TYPE_ACTIVE_ENERGY)).respond(
        200, json={"rollupDataPoints": []}
    )

    result = _make_client(env_file).fetch_day(day)
    # Apple's RHR was discarded; resting_hr stays None.
    assert result.metrics.resting_hr is None


@respx.mock
def test_fetch_day_sends_no_filter_param(env_file: Path) -> None:
    """The list endpoints must NOT include a `filter` query param — Google's
    AIP-160 implementation rejects every variant we tried, so we post-filter
    client-side instead.
    """
    day = date(2026, 6, 22)
    _stub_token()
    empty = {"dataPoints": []}
    route = respx.get(BASE_URL + _list_path(TYPE_HRV)).respond(200, json=empty)
    for p in (TYPE_RHR, TYPE_RESPIRATORY, TYPE_SPO2, TYPE_SLEEP):
        respx.get(BASE_URL + _list_path(p)).respond(200, json=empty)
    respx.post(BASE_URL + _rollup_path(TYPE_STEPS)).respond(
        200, json={"rollupDataPoints": []}
    )
    respx.post(BASE_URL + _rollup_path(TYPE_ACTIVE_ENERGY)).respond(
        200, json={"rollupDataPoints": []}
    )

    _make_client(env_file).fetch_day(day)
    params = dict(route.calls.last.request.url.params)
    assert "filter" not in params
    assert params.get("pageSize") == "100"


# --- fetch_workouts ---------------------------------------------------------


@respx.mock
def test_fetch_workouts_maps_fitbit_exercise(env_file: Path) -> None:
    """One Fitbit exercise point is mapped; a non-Fitbit point is filtered out."""
    since = until = date(2026, 6, 26)
    _stub_token()

    workout_body = {
        "dataPoints": [
            {
                # Fitbit workout — should be included
                "name": "users/me/dataTypes/exercise/dataPoints/333382904545068560",
                "dataSource": {
                    "recordingMethod": "ACTIVELY_MEASURED",
                    "device": {"formFactor": "FITNESS_BAND"},
                    "platform": "FITBIT",
                },
                "exercise": {
                    "interval": {
                        "startTime": "2026-06-26T23:10:37Z",
                        "endTime": "2026-06-27T00:06:46Z",
                    },
                    "exerciseType": "WORKOUT",
                    "displayName": "Custom workout",
                    "metricsSummary": {
                        "caloriesKcal": 237,
                        "averageHeartRateBeatsPerMinute": "89",
                    },
                },
            },
            {
                # Non-Fitbit source — should be filtered out
                "name": "users/me/dataTypes/exercise/dataPoints/999999999999999999",
                "dataSource": {"platform": "APPLE_HEALTH"},
                "exercise": {
                    "interval": {
                        "startTime": "2026-06-26T15:00:00Z",
                        "endTime": "2026-06-26T16:00:00Z",
                    },
                    "exerciseType": "RUN",
                    "displayName": "Morning run",
                    "metricsSummary": {
                        "caloriesKcal": 500,
                        "averageHeartRateBeatsPerMinute": "140",
                    },
                },
            },
        ]
    }

    respx.get(
        url__regex=r"/dataTypes/exercise/dataPoints"
    ).respond(200, json=workout_body)

    result = _make_client(env_file).fetch_workouts(since, until)

    # Only the Fitbit workout should make it through
    assert len(result.workouts) == 1
    w = result.workouts[0]
    assert w.device == "google_health"
    assert w.provider_id == "333382904545068560"
    assert w.sport == "Custom workout"
    assert w.avg_hr == 89.0  # string "89" converted to float
    assert w.max_hr is None  # Google Health v4 does not expose per-session max HR
    assert w.calories == 237
    assert w.duration_minutes == 56  # (00:06:46 - 23:10:37) = 3369s → round(56.15) = 56
    assert w.date == date(2026, 6, 26)  # start_dt in LA timezone is 2026-06-26

    # Raw payload should always be present (one entry)
    assert len(result.raw) == 1
    assert result.raw[0].endpoint == _list_path(TYPE_WORKOUT)
