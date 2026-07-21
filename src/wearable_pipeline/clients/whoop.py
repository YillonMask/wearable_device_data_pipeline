"""Whoop 5.0 client.

Whoop's data model is cycle-based: a sleep cycle runs sleep-to-sleep (not
midnight-to-midnight), recovery is the analysis of the sleep just ended, and
day strain belongs to the awake period of a cycle. To map onto a calendar
day, we anchor on the **wake-up date in the user's local timezone**: a sleep
record contributes to ``day = D`` iff its ``end`` timestamp, converted to
``LOCAL_TIMEZONE``, falls on ``D``. The recovery for that sleep and the
cycle whose id matches drive the rest of ``DailyMetrics``.

API base: ``https://api.prod.whoop.com/developer/v1/``. Authentication is via
the shared ``TokenManager`` (OAuth 2.0 authorization-code flow). The Whoop API
does not expose step count or active calories directly; we convert ``kilojoule``
from the cycle score to kcal for ``active_calories``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..models import DailyMetrics, FetchResult, RawPayload, Workout, WorkoutFetchResult
from ._oauth import TokenManager

BASE_URL = "https://api.prod.whoop.com/developer/"
# Whoop's developer API moved to v2 — v1 cycle still resolves for backward
# compat but v1 recovery and v1 activity/sleep return 404. Use v2 everywhere.
CYCLE_ENDPOINT = "v2/cycle"
RECOVERY_ENDPOINT = "v2/recovery"
SLEEP_ENDPOINT = "v2/activity/sleep"
WORKOUT_ENDPOINT = "v2/activity/workout"
_TOKEN_ENDPOINT = "https://api.prod.whoop.com/oauth/oauth2/token"

_QUERY_LIMIT = 25  # Whoop docs cap at 25 per page; 2-day window comfortably fits.
_KJ_PER_KCAL = 4.184


class WhoopClient:
    device = "whoop"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        env_path: Path,
        local_timezone: str = "America/Los_Angeles",
        http_client: httpx.Client | None = None,
    ) -> None:
        self._tz = ZoneInfo(local_timezone)
        self._injected_client = http_client
        self._token_mgr = TokenManager(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            token_endpoint=_TOKEN_ENDPOINT,
            env_key="WHOOP_REFRESH_TOKEN",
            env_path=env_path,
            http_client=http_client,
        )

    def fetch_day(self, day: date) -> FetchResult:
        window_start, window_end = self._utc_window(day)
        params = {
            "start": _iso_utc(window_start),
            "end": _iso_utc(window_end),
            "limit": _QUERY_LIMIT,
        }

        raw: list[RawPayload] = []
        responses: dict[str, dict[str, Any]] = {}

        client, owned = self._open_client()
        try:
            for path in (CYCLE_ENDPOINT, RECOVERY_ENDPOINT, SLEEP_ENDPOINT):
                body = self._authed_get(client, path, params)
                raw.append(
                    RawPayload(endpoint=path, date=day.isoformat(), payload=body)
                )
                responses[path] = body
        finally:
            if owned:
                client.close()

        metrics = self._normalize(day, responses)
        return FetchResult(metrics=metrics, raw=raw)

    def fetch_workouts(self, since: date, until: date) -> WorkoutFetchResult:
        window_start, _ = self._utc_window(since)
        _, window_end = self._utc_window(until)
        params = {
            "start": _iso_utc(window_start),
            "end": _iso_utc(window_end),
            "limit": _QUERY_LIMIT,
        }

        raw: list[RawPayload] = []
        records: list[dict[str, Any]] = []
        client, owned = self._open_client()
        try:
            next_token: str | None = None
            while True:
                page_params = dict(params)
                if next_token:
                    page_params["nextToken"] = next_token
                body = self._authed_get(client, WORKOUT_ENDPOINT, page_params)
                raw.append(
                    RawPayload(
                        endpoint=WORKOUT_ENDPOINT,
                        date=f"{since.isoformat()}..{until.isoformat()}",
                        payload=body,
                    )
                )
                records.extend(body.get("records") or [])
                next_token = body.get("next_token")
                if not next_token:
                    break
        finally:
            if owned:
                client.close()

        workouts = [self._normalize_workout(r) for r in records]
        workouts = [w for w in workouts if w is not None]
        return WorkoutFetchResult(workouts=workouts, raw=raw)

    def _normalize_workout(self, record: dict[str, Any]) -> Workout | None:
        provider_id = record.get("id")
        start = record.get("start")
        if not provider_id or not start:
            return None
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end = record.get("end")
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None
        duration = (
            round((end_dt - start_dt).total_seconds() / 60) if end_dt else None
        )
        score = record.get("score") or {}
        return Workout(
            device=self.device,
            provider_id=str(provider_id),
            sport=record.get("sport_name"),
            start_time=start_dt,
            end_time=end_dt,
            duration_minutes=duration,
            avg_hr=score.get("average_heart_rate"),
            max_hr=score.get("max_heart_rate"),
            calories=_kj_to_kcal(score.get("kilojoule")),
            date=start_dt.astimezone(self._tz).date(),
        )

    def _utc_window(self, day: date) -> tuple[datetime, datetime]:
        # Local midnight on day-1 to local midnight on day+2.
        local_start = datetime(day.year, day.month, day.day, tzinfo=self._tz) - timedelta(days=1)
        local_end = datetime(day.year, day.month, day.day, tzinfo=self._tz) + timedelta(days=2)
        return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)

    def _open_client(self) -> tuple[httpx.Client, bool]:
        if self._injected_client is not None:
            return self._injected_client, False
        return (
            httpx.Client(base_url=BASE_URL, timeout=30.0),
            True,
        )

    def _authed_get(
        self, client: httpx.Client, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        access = self._token_mgr.get_access_token()
        headers = {"Authorization": f"Bearer {access}"}
        resp = client.get(path, params=params, headers=headers)
        if resp.status_code == 401:
            # Force a refresh and retry once.
            self._token_mgr._refresh()  # type: ignore[attr-defined]
            headers = {"Authorization": f"Bearer {self._token_mgr.get_access_token()}"}
            resp = client.get(path, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _normalize(
        self, day: date, responses: dict[str, dict[str, Any]]
    ) -> DailyMetrics:
        sleep = _pick_main_sleep(
            responses[SLEEP_ENDPOINT].get("records") or [], day, self._tz
        )
        if sleep is None:
            return DailyMetrics(date=day, device=self.device)

        sleep_id = sleep.get("id")
        cycle_id = sleep.get("cycle_id")
        recovery = _first_matching(
            responses[RECOVERY_ENDPOINT].get("records") or [],
            "sleep_id",
            sleep_id,
        )
        cycle = _first_matching(
            responses[CYCLE_ENDPOINT].get("records") or [], "id", cycle_id
        )

        sleep_score = sleep.get("score") or {}
        stages = sleep_score.get("stage_summary") or {}
        rec_score = (recovery or {}).get("score") or {}
        cyc_score = (cycle or {}).get("score") or {}

        return DailyMetrics(
            date=day,
            device=self.device,
            total_sleep_minutes=_milli_to_min(_total_asleep_ms(stages)),
            sleep_efficiency=_pct_to_unit(
                sleep_score.get("sleep_efficiency_percentage")
            ),
            sleep_latency_minutes=None,  # Whoop does not expose latency separately
            rem_minutes=_milli_to_min(stages.get("total_rem_sleep_time_milli")),
            deep_minutes=_milli_to_min(
                stages.get("total_slow_wave_sleep_time_milli")
            ),
            light_minutes=_milli_to_min(
                stages.get("total_light_sleep_time_milli")
            ),
            awake_minutes=_milli_to_min(stages.get("total_awake_time_milli")),
            sleep_score=_round_int(sleep_score.get("sleep_performance_percentage")),
            readiness_score=_round_int(rec_score.get("recovery_score")),
            hrv_ms=rec_score.get("hrv_rmssd_milli"),
            resting_hr=rec_score.get("resting_heart_rate"),
            respiratory_rate=sleep_score.get("respiratory_rate"),
            skin_temp=rec_score.get("skin_temp_celsius"),
            strain_or_activity_score=cyc_score.get("strain"),
            active_calories=_kj_to_kcal(cyc_score.get("kilojoule")),
            steps=None,  # not exposed in the Whoop public API
        )


def _pick_main_sleep(
    records: list[dict[str, Any]], day: date, tz: ZoneInfo
) -> dict[str, Any] | None:
    candidates = [
        r
        for r in records
        if not r.get("nap", False) and _end_local_date(r, tz) == day
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("end") or "")


def _end_local_date(record: dict[str, Any], tz: ZoneInfo) -> date | None:
    end = record.get("end")
    if not end:
        return None
    dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return dt.astimezone(tz).date()


def _first_matching(
    records: list[dict[str, Any]], key: str, value: Any
) -> dict[str, Any] | None:
    if value is None:
        return None
    return next((r for r in records if r.get(key) == value), None)


def _total_asleep_ms(stages: dict[str, Any]) -> int | None:
    total_in_bed = stages.get("total_in_bed_time_milli")
    awake = stages.get("total_awake_time_milli")
    if total_in_bed is None:
        return None
    if awake is None:
        return total_in_bed
    return total_in_bed - awake


def _milli_to_min(ms: int | float | None) -> int | None:
    if ms is None:
        return None
    return round(ms / 60000)


def _pct_to_unit(pct: int | float | None) -> float | None:
    if pct is None:
        return None
    return pct / 100


def _round_int(value: int | float | None) -> int | None:
    if value is None:
        return None
    return round(value)


def _kj_to_kcal(kj: int | float | None) -> int | None:
    if kj is None:
        return None
    return round(kj / _KJ_PER_KCAL)


def _iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
