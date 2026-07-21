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

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..models import DailyMetrics, FetchResult, RawPayload, Workout, WorkoutFetchResult
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

_DEFAULT_PAGE_SIZE = 100


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
        raw: list[RawPayload] = []
        client, owned = self._open_client()
        try:
            body = self._authed_request(
                client, "GET", _list_path(TYPE_WORKOUT),
                params={"pageSize": _DEFAULT_PAGE_SIZE},
            )
        finally:
            if owned:
                client.close()
        raw.append(
            RawPayload(
                endpoint=_list_path(TYPE_WORKOUT),
                date=f"{since.isoformat()}..{until.isoformat()}",
                payload=body,
            )
        )
        workouts = []
        for dp in body.get("dataPoints") or []:
            w = self._normalize_workout(dp, since, until)
            if w is not None:
                workouts.append(w)
        return WorkoutFetchResult(workouts=workouts, raw=raw)

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
