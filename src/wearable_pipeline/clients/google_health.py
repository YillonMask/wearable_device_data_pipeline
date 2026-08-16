"""Google Health API v4 client — Fitbit Air ingestion.

This is the Google Health API (``https://health.googleapis.com/v4/``), NOT
the legacy Fitbit Web API. For a single-user app the OAuth consent screen
must remain in Testing mode with the user as a test user; otherwise the
Restricted scopes require CASA review.

Access patterns (per the v4 discovery document + empirical verification):

  * Daily summary types (``daily-heart-rate-variability``,
    ``daily-resting-heart-rate``, ``daily-respiratory-rate``,
    ``daily-oxygen-saturation``) → ``GET dataPoints`` *without* a server-side
    filter. The API's AIP-160 implementation rejects every date-filter
    syntax we tried (``INVALID_DATA_POINT_FILTER_*``); we fetch a page and
    post-filter on the response's nested ``date`` proto ``{year, month, day}``.
  * Sleep (``sleep``) → ``GET dataPoints`` then post-filter to records whose
    ``interval.endTime`` lands on the target day in ``local_timezone``.
  * Steps and ``active-energy-burned`` → ``POST dataPoints:dailyRollUp``
    with a ``CivilTimeInterval`` (closed-open range, ``windowSizeDays=1``).

We deliberately do NOT call ``dataPoints:reconcile`` — that's Google Health's
merged-source stream. We pull only Fitbit's own measurements so each device's
numbers are independently comparable.

Source filtering: real responses tag Fitbit data with ``platform: "FITBIT"``
inside the ``dataSource`` object. ``_is_fitbit_source`` keys off that.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..models import (
    DailyMetrics,
    FetchResult,
    HeartRateSeries,
    RawPayload,
    Workout,
    WorkoutFetchResult,
)
from ._oauth import TokenManager

BASE_URL = "https://health.googleapis.com/v4/"
USER = "users/me"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

TYPE_HRV = "daily-heart-rate-variability"
TYPE_RHR = "daily-resting-heart-rate"
TYPE_RESPIRATORY = "daily-respiratory-rate"
TYPE_SPO2 = "daily-oxygen-saturation"
TYPE_SLEEP = "sleep"
DAILY_LIST_TYPES = (TYPE_HRV, TYPE_RHR, TYPE_RESPIRATORY, TYPE_SPO2)

TYPE_STEPS = "steps"
TYPE_ACTIVE_ENERGY = "active-energy-burned"
ROLLUP_TYPES = (TYPE_STEPS, TYPE_ACTIVE_ENERGY)

TYPE_WORKOUT = "exercise"

# Per-sample intraday HR. `HeartRate` is a *sample* data type in the v4 schema
# (`sampleTime` + `beatsPerMinute`), not an interval or daily-summary one.
TYPE_HEART_RATE = "heart-rate"

# Server-side time filtering DOES work on dataPoints:list — but the field family
# has to match the data type's shape, and the field name is snake_case even
# though the data type id is hyphenated (`heart-rate` -> `heart_rate`). Sample
# types filter on `sample_time.physical_time`; interval types on
# `interval.start_time`; daily summaries on `date`. Using the wrong family is
# what returns INVALID_DATA_POINT_FILTER_DATA_TYPE_RESTRICTION.
_HR_FILTER_FIELD = "heart_rate.sample_time.physical_time"
# `exercise` is a *session* type, so it filters on civil (local) start time.
_WORKOUT_FILTER_FIELD = "exercise.interval.civil_start_time"

_DEFAULT_PAGE_SIZE = 100
# dataPoints:list caps pageSize at 10000 (except exercise/sleep, capped at 25).
# Intraday HR is dense, so ask for the max rather than the 100 used elsewhere.
_MAX_PAGE_SIZE = 10000
# ...except `exercise` and `sleep`, which Google caps at 25 per page. Asking for
# more is silently truncated, so a wide window MUST paginate.
_EXERCISE_PAGE_SIZE = 25
_MAX_PAGES = 50  # guard against an unbounded nextPageToken loop


class GoogleHealthClient:
    device = "google_health"

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
            env_key="GOOGLE_HEALTH_REFRESH_TOKEN",
            env_path=env_path,
            http_client=http_client,
        )

    def fetch_day(self, day: date) -> FetchResult:
        raw: list[RawPayload] = []
        responses: dict[str, dict[str, Any]] = {}

        client, owned = self._open_client()
        try:
            for data_type in DAILY_LIST_TYPES + (TYPE_SLEEP,):
                body = self._list_data_points(client, data_type)
                responses[data_type] = body
                raw.append(
                    RawPayload(
                        endpoint=_list_path(data_type),
                        date=day.isoformat(),
                        payload=body,
                    )
                )

            for data_type in ROLLUP_TYPES:
                body = self._daily_rollup(client, data_type, day)
                responses[data_type] = body
                raw.append(
                    RawPayload(
                        endpoint=_rollup_path(data_type),
                        date=day.isoformat(),
                        payload=body,
                    )
                )
        finally:
            if owned:
                client.close()

        metrics = self._normalize(day, responses)
        return FetchResult(metrics=metrics, raw=raw)

    def fetch_workouts(self, since: date, until: date) -> WorkoutFetchResult:
        """Fitbit workouts whose local start date falls in ``[since, until]``.

        Three things have to line up here or workouts go missing *silently*:

        * `exercise` is capped at **25** per page (not the 10000 other types
          allow), so anything wider than a couple of days needs pagination.
        * Results come back newest-first, so without a server-side filter a
          backfill of an older range would just get the 25 most recent
          workouts and match none of them.
        * Sessions filter on **civil** (local) start time, and the upper bound
          is exclusive — hence `until + 1 day` to keep `until` inclusive for
          the caller.
        """
        time_filter = (
            f'{_WORKOUT_FILTER_FIELD} >= "{since.isoformat()}" AND '
            f'{_WORKOUT_FILTER_FIELD} < "{(until + timedelta(days=1)).isoformat()}"'
        )
        raw: list[RawPayload] = []
        data_points: list[dict[str, Any]] = []

        client, owned = self._open_client()
        try:
            page_token: str | None = None
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {
                    "pageSize": _EXERCISE_PAGE_SIZE,
                    "filter": time_filter,
                }
                if page_token:
                    params["pageToken"] = page_token
                body = self._authed_request(
                    client, "GET", _list_path(TYPE_WORKOUT), params=params
                )
                # One raw row per page — WorkoutFetchResult.raw is already a
                # list, so nothing needs to change downstream.
                raw.append(
                    RawPayload(
                        endpoint=_list_path(TYPE_WORKOUT),
                        date=f"{since.isoformat()}..{until.isoformat()}",
                        payload=body,
                    )
                )
                data_points.extend(body.get("dataPoints") or [])
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
        finally:
            if owned:
                client.close()

        workouts = []
        for dp in data_points:
            w = self._normalize_workout(dp, since, until)
            if w is not None:
                workouts.append(w)
        return WorkoutFetchResult(workouts=workouts, raw=raw)

    def fetch_heart_rate(self, start: datetime, end: datetime) -> HeartRateSeries:
        """Per-sample Fitbit HR covering ``[start, end)`` (tz-aware).

        Fitbit's intraday cadence is roughly one sample per 2 s (verified live
        2026-08-15) — nothing like Garmin's true 1 Hz. Measure the delivered
        spacing rather than assuming a rate. The Fitbit Air records nothing at
        all while swimming, so a swim window comes back near-empty.

        Paginated responses are archived together under a single ``pages`` key
        so the raw record stays one row per fetch, matching the other clients.
        """
        samples: list[tuple[datetime, int]] = []
        pages: list[dict[str, Any]] = []
        time_filter = (
            f'{_HR_FILTER_FIELD} >= "{_rfc3339(start)}" AND '
            f'{_HR_FILTER_FIELD} < "{_rfc3339(end)}"'
        )

        client, owned = self._open_client()
        try:
            page_token: str | None = None
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {
                    "pageSize": _MAX_PAGE_SIZE,
                    "filter": time_filter,
                }
                if page_token:
                    params["pageToken"] = page_token
                body = self._authed_request(
                    client, "GET", _list_path(TYPE_HEART_RATE), params=params
                )
                pages.append(body)
                for dp in body.get("dataPoints") or []:
                    sample = _normalize_hr_sample(dp)
                    if sample is not None:
                        samples.append(sample)
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
        finally:
            if owned:
                client.close()

        # The API returns newest-first; downstream wants chronological order.
        samples.sort(key=lambda pair: pair[0])
        raw = RawPayload(
            endpoint=_list_path(TYPE_HEART_RATE),
            date=start.date().isoformat(),
            payload={"pages": pages},
        )
        return HeartRateSeries(samples=samples, raw=raw)

    def _normalize_workout(self, dp: dict[str, Any], since: date, until: date) -> Workout | None:
        if not _is_fitbit_source(dp.get("dataSource")):
            return None
        provider_id = (dp.get("name") or "").split("/")[-1]
        ex = dp.get("exercise") or {}
        interval = ex.get("interval") or {}
        start = interval.get("startTime")
        if not provider_id or not start:
            return None
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        local_date = start_dt.astimezone(self._tz).date()
        if not (since <= local_date <= until):
            return None
        end = interval.get("endTime")
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None
        duration = round((end_dt - start_dt).total_seconds() / 60) if end_dt else None
        msum = ex.get("metricsSummary") or {}
        return Workout(
            device=self.device,
            provider_id=provider_id,
            sport=ex.get("displayName") or ex.get("exerciseType"),
            start_time=start_dt,
            end_time=end_dt,
            duration_minutes=duration,
            avg_hr=_to_float(msum.get("averageHeartRateBeatsPerMinute")),
            max_hr=None,  # Google Health v4 does not expose per-session max HR
            calories=_int_or_none(msum.get("caloriesKcal")),
            date=local_date,
        )

    def _open_client(self) -> tuple[httpx.Client, bool]:
        if self._injected_client is not None:
            return self._injected_client, False
        return httpx.Client(base_url=BASE_URL, timeout=30.0), True

    def _authed_request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token_mgr.get_access_token()}"}
        resp = client.request(method, path, params=params, json=json_body, headers=headers)
        if resp.status_code == 401:
            self._token_mgr._refresh()  # type: ignore[attr-defined]
            headers = {"Authorization": f"Bearer {self._token_mgr.get_access_token()}"}
            resp = client.request(method, path, params=params, json=json_body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _list_data_points(
        self, client: httpx.Client, data_type: str
    ) -> dict[str, Any]:
        # No server-side filter: Google Health's filter language rejects all
        # date/time syntaxes we tried. Fetch a page, filter client-side.
        return self._authed_request(
            client,
            "GET",
            _list_path(data_type),
            params={"pageSize": _DEFAULT_PAGE_SIZE},
        )

    def _daily_rollup(
        self, client: httpx.Client, data_type: str, day: date
    ) -> dict[str, Any]:
        tomorrow = day + timedelta(days=1)
        # CivilTimeInterval has fields named `start`/`end`, each a CivilDateTime
        # which wraps a `date: {year, month, day}`. The flat year/month/day
        # shape Google's REST API rejects with a "Cannot find field" error.
        body = {
            "range": {
                "start": _civil_datetime(day),
                "end": _civil_datetime(tomorrow),
            },
            "windowSizeDays": 1,
            "pageSize": 10,
        }
        return self._authed_request(
            client, "POST", _rollup_path(data_type), json_body=body
        )

    def _normalize(
        self, day: date, responses: dict[str, dict[str, Any]]
    ) -> DailyMetrics:
        hrv = _pick_fitbit_daily(
            responses[TYPE_HRV],
            day,
            payload_key="dailyHeartRateVariability",
            value_key="averageHeartRateVariabilityMilliseconds",
        )
        rhr = _pick_fitbit_daily(
            responses[TYPE_RHR],
            day,
            payload_key="dailyRestingHeartRate",
            value_key="beatsPerMinute",
            cast=_to_float,
        )
        respiratory = _pick_fitbit_daily(
            responses[TYPE_RESPIRATORY],
            day,
            payload_key="dailyRespiratoryRate",
            value_key="breathsPerMinute",
        )
        sleep_summary = _summarize_sleep_for_day(
            responses[TYPE_SLEEP], day, self._tz
        )
        # Rollup values use `countSum` / `kcalSum` (the *RollupValue schemas),
        # not the per-sample `count` / `kcal` fields.
        steps = _rollup_int(
            responses[TYPE_STEPS], rollup_key="steps", value_key="countSum"
        )
        active_cal = _rollup_int(
            responses[TYPE_ACTIVE_ENERGY],
            rollup_key="activeEnergyBurned",
            value_key="kcalSum",
        )

        return DailyMetrics(
            date=day,
            device=self.device,
            total_sleep_minutes=sleep_summary.get("total_sleep_minutes"),
            sleep_efficiency=sleep_summary.get("sleep_efficiency"),
            sleep_latency_minutes=sleep_summary.get("sleep_latency_minutes"),
            rem_minutes=sleep_summary.get("rem_minutes"),
            deep_minutes=sleep_summary.get("deep_minutes"),
            light_minutes=sleep_summary.get("light_minutes"),
            awake_minutes=sleep_summary.get("awake_minutes"),
            sleep_score=None,
            readiness_score=None,
            hrv_ms=hrv,
            resting_hr=rhr,
            respiratory_rate=respiratory,
            body_temp_deviation=None,
            skin_temp=None,
            strain_or_activity_score=None,
            active_calories=active_cal,
            steps=steps,
        )


# ---------------------------------------------------------------------------
# helpers


def _list_path(data_type: str) -> str:
    return f"{USER}/dataTypes/{data_type}/dataPoints"


def _rollup_path(data_type: str) -> str:
    return f"{USER}/dataTypes/{data_type}/dataPoints:dailyRollUp"


def _civil_datetime(d: date) -> dict[str, Any]:
    """CivilDateTime proto: {date: {year, month, day}, time?: TimeOfDay}.

    Time-of-day is optional and defaults to midnight; we omit it so the
    interval covers a full calendar day.
    """
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


def _date_proto_matches(date_proto: dict[str, Any] | None, day: date) -> bool:
    if not date_proto:
        return False
    return (
        date_proto.get("year") == day.year
        and date_proto.get("month") == day.month
        and date_proto.get("day") == day.day
    )


def _rfc3339(dt: datetime) -> str:
    """Filter literals must be RFC-3339 with an explicit Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_hr_sample(dp: dict[str, Any]) -> tuple[datetime, int] | None:
    """One `heart-rate` data point -> (utc_datetime, bpm), or None to skip.

    Source-filtered to Fitbit: the same Google Health account can carry samples
    from a Pixel Watch or a phone, and mixing those into a Fitbit series would
    silently corrupt the comparison.
    """
    if not _is_fitbit_source(dp.get("dataSource")):
        return None
    hr = dp.get("heartRate") or {}
    # beatsPerMinute is a *string* in the v4 schema, like most numerics here.
    bpm = _to_float(hr.get("beatsPerMinute"))
    physical = (hr.get("sampleTime") or {}).get("physicalTime")
    if bpm is None or bpm <= 0 or not isinstance(physical, str):
        return None
    try:
        ts = datetime.fromisoformat(physical.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts.astimezone(timezone.utc), round(bpm)


def _is_fitbit_source(data_source: dict[str, Any] | None) -> bool:
    """Real Google Health responses tag Fitbit data with ``platform: 'FITBIT'``."""
    if not data_source:
        # Some endpoints omit dataSource on rollups; be permissive there.
        return True
    if data_source.get("platform") == "FITBIT":
        return True
    # Legacy/alternate shapes: check manufacturer or app id text.
    candidate = (
        str(data_source.get("applicationDataSourceId", ""))
        + " "
        + str((data_source.get("device") or {}).get("manufacturer", ""))
    )
    return "fitbit" in candidate.lower()


def _pick_fitbit_daily(
    list_response: dict[str, Any],
    day: date,
    *,
    payload_key: str,
    value_key: str,
    cast: Any = None,
) -> Any:
    for dp in list_response.get("dataPoints") or []:
        if not _is_fitbit_source(dp.get("dataSource")):
            continue
        payload = dp.get(payload_key) or {}
        if not _date_proto_matches(payload.get("date"), day):
            continue
        value = payload.get(value_key)
        if value is None:
            continue
        return cast(value) if cast else value
    return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _summarize_sleep_for_day(
    list_response: dict[str, Any], day: date, tz: ZoneInfo
) -> dict[str, Any]:
    """Pick the longest Fitbit-sourced sleep ending on the target local date."""
    candidates: list[dict[str, Any]] = []
    for dp in list_response.get("dataPoints") or []:
        if not _is_fitbit_source(dp.get("dataSource")):
            continue
        sleep = dp.get("sleep") or {}
        interval = sleep.get("interval") or {}
        end = interval.get("endTime") or interval.get("startTime")
        if not end:
            continue
        try:
            dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.astimezone(tz).date() == day:
            candidates.append(dp)
    if not candidates:
        return {}

    def asleep_minutes(dp: dict[str, Any]) -> int:
        return int(((dp.get("sleep") or {}).get("summary") or {}).get("minutesAsleep") or 0)

    main = max(candidates, key=asleep_minutes)
    summary = (main.get("sleep") or {}).get("summary") or {}

    stages = {
        s.get("type"): _int_or_none(s.get("minutes"))
        for s in (summary.get("stagesSummary") or [])
    }

    asleep = _int_or_none(summary.get("minutesAsleep"))
    awake = _int_or_none(summary.get("minutesAwake"))
    in_period = _int_or_none(summary.get("minutesInSleepPeriod"))
    latency = _int_or_none(summary.get("minutesToFallAsleep"))
    efficiency = None
    if asleep is not None and in_period and in_period > 0:
        efficiency = asleep / in_period

    return {
        "total_sleep_minutes": asleep,
        "sleep_efficiency": efficiency,
        "sleep_latency_minutes": latency,
        "rem_minutes": stages.get("REM"),
        "deep_minutes": stages.get("DEEP"),
        "light_minutes": stages.get("LIGHT"),
        "awake_minutes": stages.get("AWAKE") or awake,
    }


def _rollup_int(
    rollup_response: dict[str, Any], *, rollup_key: str, value_key: str
) -> int | None:
    total: int | None = None
    for point in rollup_response.get("rollupDataPoints") or []:
        rollup = point.get(rollup_key) or {}
        raw_value = rollup.get(value_key)
        if raw_value is None:
            inner = rollup.get("total") or rollup.get("value") or {}
            if isinstance(inner, dict):
                raw_value = inner.get(value_key)
        if raw_value is None:
            continue
        try:
            n = int(round(float(raw_value)))
        except (TypeError, ValueError):
            continue
        total = (total or 0) + n
    return total
