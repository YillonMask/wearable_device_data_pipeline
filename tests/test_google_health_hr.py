from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from wearable_pipeline.clients.google_health import (
    BASE_URL,
    TYPE_HEART_RATE,
    TYPE_WORKOUT,
    GoogleHealthClient,
    _list_path,
    _normalize_hr_sample,
    _rfc3339,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
HR_URL = f"{BASE_URL}{_list_path(TYPE_HEART_RATE)}"

START = datetime(2026, 8, 11, 4, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)


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


def _fitbit_point(offset_s: int, bpm: int) -> dict:
    ts = START + timedelta(seconds=offset_s)
    return {
        "dataSource": {"platform": "FITBIT"},
        "heartRate": {
            # beatsPerMinute is a string in the v4 schema
            "beatsPerMinute": str(bpm),
            "sampleTime": {"physicalTime": ts.strftime("%Y-%m-%dT%H:%M:%SZ")},
        },
    }


def _mock_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    )


# --- sample normalization ----------------------------------------------------


def test_normalize_parses_string_bpm_and_physical_time() -> None:
    sample = _normalize_hr_sample(_fitbit_point(0, 120))
    assert sample == (START, 120)


def test_normalize_rejects_non_fitbit_sources() -> None:
    """A Pixel Watch on the same account must not pollute the Fitbit series."""
    point = _fitbit_point(0, 120)
    point["dataSource"] = {"platform": "GOOGLE_PIXEL_WATCH"}
    assert _normalize_hr_sample(point) is None


def test_normalize_skips_malformed_points() -> None:
    missing_bpm = _fitbit_point(0, 120)
    del missing_bpm["heartRate"]["beatsPerMinute"]
    assert _normalize_hr_sample(missing_bpm) is None

    zero = _fitbit_point(0, 0)
    assert _normalize_hr_sample(zero) is None

    bad_time = _fitbit_point(0, 120)
    bad_time["heartRate"]["sampleTime"] = {"physicalTime": "not-a-time"}
    assert _normalize_hr_sample(bad_time) is None

    assert _normalize_hr_sample({}) is None


def test_rfc3339_forces_utc_z() -> None:
    assert _rfc3339(START) == "2026-08-11T04:10:00Z"
    pacific = timezone(timedelta(hours=-7))
    assert _rfc3339(datetime(2026, 8, 10, 21, 10, tzinfo=pacific)) == "2026-08-11T04:10:00Z"


# --- fetch -------------------------------------------------------------------


@respx.mock
def test_fetch_heart_rate_sends_sample_time_filter_and_max_page_size(env_file) -> None:
    _mock_token()
    route = respx.get(HR_URL).mock(
        return_value=httpx.Response(
            200, json={"dataPoints": [_fitbit_point(0, 120), _fitbit_point(5, 122)]}
        )
    )

    series = _make_client(env_file).fetch_heart_rate(START, END)

    assert [bpm for _, bpm in series.samples] == [120, 122]
    params = dict(route.calls[0].request.url.params)
    assert params["pageSize"] == "10000"
    # Sample data type -> sample_time.physical_time, snake_case, hyphens gone.
    assert params["filter"] == (
        'heart_rate.sample_time.physical_time >= "2026-08-11T04:10:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-08-11T04:40:00Z"'
    )


@respx.mock
def test_fetch_heart_rate_follows_pagination_and_archives_every_page(env_file) -> None:
    _mock_token()
    respx.get(HR_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"dataPoints": [_fitbit_point(0, 120)], "nextPageToken": "p2"},
            ),
            httpx.Response(200, json={"dataPoints": [_fitbit_point(5, 122)]}),
        ]
    )

    series = _make_client(env_file).fetch_heart_rate(START, END)

    assert [bpm for _, bpm in series.samples] == [120, 122]
    assert len(series.raw.payload["pages"]) == 2


@respx.mock
def test_fetch_heart_rate_sorts_newest_first_responses(env_file) -> None:
    """The API returns interval-start descending; downstream wants ascending."""
    _mock_token()
    respx.get(HR_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "dataPoints": [
                    _fitbit_point(10, 125),
                    _fitbit_point(5, 122),
                    _fitbit_point(0, 120),
                ]
            },
        )
    )

    series = _make_client(env_file).fetch_heart_rate(START, END)

    assert [bpm for _, bpm in series.samples] == [120, 122, 125]
    assert series.samples[0][0] < series.samples[-1][0]


# --- fetch_workouts paging / filtering ---------------------------------------

WORKOUT_URL = f"{BASE_URL}{_list_path(TYPE_WORKOUT)}"


def _exercise_point(idx: int, day: str = "2026-06-26") -> dict:
    return {
        "name": f"users/me/dataTypes/exercise/dataPoints/{idx}",
        "dataSource": {"platform": "FITBIT"},
        "exercise": {
            "interval": {
                "startTime": f"{day}T17:00:0{idx % 10}Z",
                "endTime": f"{day}T17:30:00Z",
            },
            "exerciseType": "WORKOUT",
            "displayName": f"w{idx}",
            "metricsSummary": {},
        },
    }


@respx.mock
def test_fetch_workouts_uses_civil_filter_and_the_25_cap(env_file) -> None:
    """exercise caps at 25/page; asking for more is silently truncated."""
    _mock_token()
    route = respx.get(WORKOUT_URL).mock(
        return_value=httpx.Response(200, json={"dataPoints": []})
    )

    _make_client(env_file).fetch_workouts(date(2026, 6, 20), date(2026, 6, 26))

    params = dict(route.calls[0].request.url.params)
    assert params["pageSize"] == "25"
    # Session type -> civil_start_time; upper bound exclusive, so until + 1 day
    assert params["filter"] == (
        'exercise.interval.civil_start_time >= "2026-06-20" AND '
        'exercise.interval.civil_start_time < "2026-06-27"'
    )


@respx.mock
def test_fetch_workouts_paginates_past_the_first_25(env_file) -> None:
    """Without paging a backfill would silently stop at 25 workouts."""
    _mock_token()
    page1 = {
        "dataPoints": [_exercise_point(i) for i in range(25)],
        "nextPageToken": "p2",
    }
    page2 = {"dataPoints": [_exercise_point(i) for i in range(25, 30)]}
    respx.get(WORKOUT_URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )

    result = _make_client(env_file).fetch_workouts(date(2026, 6, 26), date(2026, 6, 26))

    assert len(result.workouts) == 30
    assert len(result.raw) == 2  # one archived row per page


@respx.mock
def test_fetch_workouts_sends_page_token_on_later_pages(env_file) -> None:
    _mock_token()
    route = respx.get(WORKOUT_URL).mock(
        side_effect=[
            httpx.Response(200, json={"dataPoints": [], "nextPageToken": "p2"}),
            httpx.Response(200, json={"dataPoints": []}),
        ]
    )

    _make_client(env_file).fetch_workouts(date(2026, 6, 26), date(2026, 6, 26))

    assert "pageToken" not in dict(route.calls[0].request.url.params)
    assert dict(route.calls[1].request.url.params)["pageToken"] == "p2"


@respx.mock
def test_fetch_workouts_stops_at_max_pages(env_file) -> None:
    """A server that always returns a token must not loop forever."""
    _mock_token()
    route = respx.get(WORKOUT_URL).mock(
        return_value=httpx.Response(
            200, json={"dataPoints": [], "nextPageToken": "always"}
        )
    )

    _make_client(env_file).fetch_workouts(date(2026, 6, 26), date(2026, 6, 26))

    assert route.call_count == 50


@respx.mock
def test_fetch_heart_rate_empty_window(env_file) -> None:
    _mock_token()
    respx.get(HR_URL).mock(return_value=httpx.Response(200, json={}))

    series = _make_client(env_file).fetch_heart_rate(START, END)

    assert series.samples == []
    assert series.raw.payload["pages"] == [{}]
