from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from ..models import DailyMetrics, FetchResult, RawPayload

BASE_URL = "https://api.ouraring.com/v2/"

# Oura v2 endpoints we pull per day. Daily summaries give the device's score
# fields; the detailed sleep collection gives durations + physiological data.
DAILY_SLEEP = "usercollection/daily_sleep"
DAILY_READINESS = "usercollection/daily_readiness"
DAILY_ACTIVITY = "usercollection/daily_activity"
SLEEP_SESSIONS = "usercollection/sleep"

_ENDPOINTS = (DAILY_SLEEP, DAILY_READINESS, DAILY_ACTIVITY, SLEEP_SESSIONS)


class OuraClient:
    """Oura Ring client using a personal access token (single-user shortcut).

    Base URL: ``https://api.ouraring.com/v2/``
    Auth header: ``Authorization: Bearer <PAT>``.

    Durations from Oura's API are in **seconds** and are converted to minutes
    for the normalized model. ``efficiency`` is already a 0..1 decimal.
    """

    device = "oura"

    def __init__(
        self,
        personal_access_token: str,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._token = personal_access_token
        self._injected_client = http_client

    def fetch_day(self, day: date) -> FetchResult:
        # All four endpoints index by the user's local-time `day` but their
        # `start_date`/`end_date` filters apply to a timestamp field that
        # carries a timezone offset, so single-day queries silently drop
        # records (observed: zero results for days that do have data). Widen
        # the window to day-1..day+1 and post-filter on the response's `day`.
        window_start = day - timedelta(days=1)
        window_end = day + timedelta(days=1)
        target_iso = day.isoformat()

        raw: list[RawPayload] = []
        responses: dict[str, dict[str, Any]] = {}

        client, owned = self._open_client()
        try:
            for path in _ENDPOINTS:
                body = self._get(client, path, window_start, window_end)
                raw.append(
                    RawPayload(endpoint=path, date=target_iso, payload=body)
                )
                responses[path] = {
                    **body,
                    "data": [
                        item
                        for item in (body.get("data") or [])
                        if item.get("day") == target_iso
                    ],
                }
        finally:
            if owned:
                client.close()

        metrics = self._normalize(day, responses)
        return FetchResult(metrics=metrics, raw=raw)

    def _get(
        self, client: httpx.Client, path: str, start: date, end: date
    ) -> dict[str, Any]:
        resp = client.get(
            path,
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
        resp.raise_for_status()
        return resp.json()

    def _open_client(self) -> tuple[httpx.Client, bool]:
        if self._injected_client is not None:
            return self._injected_client, False
        client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
        )
        return client, True

    def _normalize(
        self, day: date, responses: dict[str, dict[str, Any]]
    ) -> DailyMetrics:
        daily_sleep = _first(responses[DAILY_SLEEP]) or {}
        daily_readiness = _first(responses[DAILY_READINESS]) or {}
        daily_activity = _first(responses[DAILY_ACTIVITY]) or {}
        sleep = _longest_sleep(responses[SLEEP_SESSIONS]) or {}

        return DailyMetrics(
            date=day,
            device=self.device,
            sleep_score=daily_sleep.get("score"),
            readiness_score=daily_readiness.get("score"),
            body_temp_deviation=daily_readiness.get("temperature_deviation"),
            strain_or_activity_score=daily_activity.get("score"),
            active_calories=daily_activity.get("active_calories"),
            steps=daily_activity.get("steps"),
            total_sleep_minutes=_sec_to_min(sleep.get("total_sleep_duration")),
            # Oura returns efficiency on a 0..100 scale; DailyMetrics stores 0..1.
            sleep_efficiency=_pct_to_unit(sleep.get("efficiency")),
            sleep_latency_minutes=_sec_to_min(sleep.get("latency")),
            rem_minutes=_sec_to_min(sleep.get("rem_sleep_duration")),
            deep_minutes=_sec_to_min(sleep.get("deep_sleep_duration")),
            light_minutes=_sec_to_min(sleep.get("light_sleep_duration")),
            awake_minutes=_sec_to_min(sleep.get("awake_time")),
            hrv_ms=sleep.get("average_hrv"),
            # `average_heart_rate` is the right cross-device analogue — Whoop
            # and Google Health both report averages. `lowest_heart_rate` is
            # available in the raw payload if a different question needs it.
            resting_hr=sleep.get("average_heart_rate"),
            respiratory_rate=sleep.get("average_breath"),
        )


def _first(envelope: dict[str, Any]) -> dict[str, Any] | None:
    items = envelope.get("data") or []
    return items[0] if items else None


def _longest_sleep(envelope: dict[str, Any]) -> dict[str, Any] | None:
    # Oura's /sleep returns every session; main sleep is the longest one (no
    # explicit nap flag in the public API, so we infer by total duration).
    items = envelope.get("data") or []
    if not items:
        return None
    return max(items, key=lambda s: s.get("total_sleep_duration") or 0)


def _sec_to_min(seconds: int | float | None) -> int | None:
    if seconds is None:
        return None
    return round(seconds / 60)


def _pct_to_unit(pct: int | float | None) -> float | None:
    if pct is None:
        return None
    return pct / 100
