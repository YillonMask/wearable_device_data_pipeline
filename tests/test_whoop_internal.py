from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from wearable_pipeline.clients.whoop_internal import (
    BASE_URL,
    METRICS_ENDPOINT,
    SIGN_IN_ENDPOINT,
    USER_ENDPOINT,
    WhoopAuthError,
    WhoopInternalClient,
    _iso_ms,
    _parse_values,
    _user_id_from,
)

SIGN_IN_URL = f"{BASE_URL}{SIGN_IN_ENDPOINT}"
USER_URL = f"{BASE_URL}{USER_ENDPOINT}"
METRICS_URL = f"{BASE_URL}{METRICS_ENDPOINT}/42"

START = datetime(2026, 8, 13, 17, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 13, 17, 0, 30, tzinfo=timezone.utc)


def _make_client() -> WhoopInternalClient:
    return WhoopInternalClient(username="me@example.com", password="hunter2")


def _hr_body() -> dict:
    # 6 s bucket spacing, matching the finest step Whoop honors.
    base_ms = int(START.timestamp() * 1000)
    return {
        "values": [
            {"time": base_ms, "data": 120},
            {"time": base_ms + 6000, "data": 131},
            {"time": base_ms + 12000, "data": 145},
        ]
    }


# --- pure helpers ------------------------------------------------------------


def test_iso_ms_formats_millisecond_zulu() -> None:
    assert _iso_ms(START) == "2026-08-13T17:00:00.000Z"


def test_iso_ms_converts_to_utc() -> None:
    pacific = timezone(timedelta(hours=-7))
    assert (
        _iso_ms(datetime(2026, 8, 13, 10, 0, 0, tzinfo=pacific))
        == "2026-08-13T17:00:00.000Z"
    )


def test_user_id_from_nested_and_flat() -> None:
    assert _user_id_from({"user": {"id": 42}}) == "42"
    assert _user_id_from({"id": 7}) == "7"
    assert _user_id_from({"nope": 1}) is None


def test_parse_values_converts_ms_to_utc_datetimes() -> None:
    samples = _parse_values(_hr_body())
    assert [bpm for _, bpm in samples] == [120, 131, 145]
    assert samples[0][0] == START
    assert samples[0][0].tzinfo is timezone.utc
    # 6 s spacing preserved
    assert (samples[1][0] - samples[0][0]).total_seconds() == 6


def test_parse_values_drops_gap_buckets_rather_than_zero_filling() -> None:
    base_ms = int(START.timestamp() * 1000)
    body = {
        "values": [
            {"time": base_ms, "data": 120},
            {"time": base_ms + 6000, "data": None},  # strap off wrist
            {"time": base_ms + 12000},  # absent entirely
            {"time": None, "data": 130},
            {"time": base_ms + 18000, "data": 140},
        ]
    }
    samples = _parse_values(body)
    assert [bpm for _, bpm in samples] == [120, 140]


def test_parse_values_sorts_out_of_order_buckets() -> None:
    base_ms = int(START.timestamp() * 1000)
    body = {
        "values": [
            {"time": base_ms + 6000, "data": 131},
            {"time": base_ms, "data": 120},
        ]
    }
    assert [bpm for _, bpm in _parse_values(body)] == [120, 131]


def test_parse_values_empty_response() -> None:
    assert _parse_values({}) == []
    assert _parse_values({"values": None}) == []


# --- HTTP behaviour ----------------------------------------------------------


@respx.mock
def test_fetch_heart_rate_signs_in_then_fetches_series() -> None:
    respx.post(SIGN_IN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt"}
        )
    )
    respx.get(USER_URL).mock(
        return_value=httpx.Response(200, json={"user": {"id": 42}})
    )
    metrics = respx.get(METRICS_URL).mock(
        return_value=httpx.Response(200, json=_hr_body())
    )

    series = _make_client().fetch_heart_rate(START, END, step=6)

    assert [bpm for _, bpm in series.samples] == [120, 131, 145]
    request = metrics.calls[0].request
    assert request.headers["Authorization"] == "Bearer at"
    assert dict(request.url.params) == {
        "name": "heart_rate",
        "start": "2026-08-13T17:00:00.000Z",
        "end": "2026-08-13T17:00:30.000Z",
        "step": "6",
        "apiVersion": "7",
    }


@respx.mock
def test_sign_in_response_carrying_user_id_skips_profile_call() -> None:
    respx.post(SIGN_IN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "user": {"id": 42}}
        )
    )
    user_route = respx.get(USER_URL).mock(
        return_value=httpx.Response(200, json={"user": {"id": 42}})
    )
    respx.get(METRICS_URL).mock(return_value=httpx.Response(200, json=_hr_body()))

    _make_client().fetch_heart_rate(START, END)

    assert not user_route.called


@respx.mock
def test_raw_payload_archives_the_untouched_response() -> None:
    respx.post(SIGN_IN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "user": {"id": 42}})
    )
    body = _hr_body()
    respx.get(METRICS_URL).mock(return_value=httpx.Response(200, json=body))

    series = _make_client().fetch_heart_rate(START, END)

    assert series.raw.payload == body
    assert series.raw.date == "2026-08-13"


@respx.mock
def test_expired_token_triggers_one_resignin_and_retry() -> None:
    sign_in = respx.post(SIGN_IN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "user": {"id": 42}})
    )
    metrics = respx.get(METRICS_URL).mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json=_hr_body()),
        ]
    )

    series = _make_client().fetch_heart_rate(START, END)

    assert len(series.samples) == 3
    assert sign_in.call_count == 2  # initial + re-auth after the 401
    assert metrics.call_count == 2


@respx.mock
def test_bad_credentials_raise_without_echoing_the_password() -> None:
    respx.post(SIGN_IN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_credentials"})
    )

    with pytest.raises(WhoopAuthError) as excinfo:
        _make_client().fetch_heart_rate(START, END)

    assert "hunter2" not in str(excinfo.value)
    assert "WHOOP_USERNAME" in str(excinfo.value)


@respx.mock
def test_sign_in_without_access_token_raises() -> None:
    respx.post(SIGN_IN_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    with pytest.raises(WhoopAuthError):
        _make_client().fetch_heart_rate(START, END)


@respx.mock
def test_server_error_propagates() -> None:
    respx.post(SIGN_IN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "user": {"id": 42}})
    )
    respx.get(METRICS_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        _make_client().fetch_heart_rate(START, END)
