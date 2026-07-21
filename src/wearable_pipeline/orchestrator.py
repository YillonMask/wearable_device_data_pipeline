"""Per-day pull orchestration with retry, logging, and partial-failure tolerance.

Devices run serially (small payloads, readable logs). Each device runs inside
``try/except``; one failure never blocks the others. The CLI uses the aggregate
result to emit a 0/1/2 exit code.

Retries cover transient classes only: ``httpx.TimeoutException``,
``httpx.NetworkError``, and HTTP 429/500/502/503/504. ``Retry-After`` is
honored. Permanent 4xx (401/403/404) propagates immediately — the client may
have its own one-shot 401-refresh-retry, but after that we treat 401 as
"creds are bad, the user must fix it".
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Protocol

import httpx

from .clients.google_health import GoogleHealthClient
from .clients.oura import OuraClient
from .clients.whoop import WhoopClient
from .config import Settings
from .models import DailyMetrics, FetchResult, Workout, WorkoutFetchResult
from .storage import upsert_daily_metrics, upsert_workout, write_raw_payload

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class _Fetcher(Protocol):
    device: str

    def fetch_day(self, day: date) -> FetchResult: ...


@dataclass(frozen=True)
class ClientEntry:
    """One device's enable/disable state.

    If ``client`` is ``None``, the device is skipped — usually because some
    credential is missing. ``reason`` is the user-facing message.
    """

    device: str
    client: _Fetcher | None
    reason: str


@dataclass(frozen=True)
class DeviceResult:
    device: str
    status: str  # "success" | "failed" | "skipped"
    error: str | None = None
    metrics: DailyMetrics | None = None


@dataclass(frozen=True)
class WorkoutResult:
    device: str
    status: str  # "success" | "failed" | "skipped"
    count: int = 0
    error: str | None = None


_WORKOUT_DEVICES = ("whoop", "google_health")


def pull_workouts(
    conn: sqlite3.Connection,
    settings,
    since: date,
    until: date,
    *,
    clients: list[ClientEntry] | None = None,
) -> list[WorkoutResult]:
    """Pull recent workout sessions for Whoop and Google Health only.

    Oura entries are skipped unconditionally. Raw payloads are written before
    upserting workouts, all in a single transaction per device.
    """
    entries = clients if clients is not None else enabled_clients(settings)
    results: list[WorkoutResult] = []
    for entry in entries:
        if entry.device not in _WORKOUT_DEVICES:
            continue  # Oura excluded; workouts are Whoop + Google only
        if entry.client is None:
            results.append(WorkoutResult(entry.device, "skipped", error=entry.reason))
            continue
        try:
            result = _call_with_retry(
                lambda e=entry: e.client.fetch_workouts(since, until), entry.device
            )
        except Exception as exc:
            log.warning(
                "%s workouts: failed — %s: %s", entry.device, type(exc).__name__, exc
            )
            results.append(WorkoutResult(entry.device, "failed", error=str(exc)))
            continue
        with conn:
            for raw in result.raw:
                write_raw_payload(
                    conn,
                    device=entry.device,
                    endpoint=raw.endpoint,
                    date=raw.date,
                    payload=raw.payload,
                )
            for w in result.workouts:
                upsert_workout(conn, w)
        log.info("%s workouts: ok (%d)", entry.device, len(result.workouts))
        results.append(WorkoutResult(entry.device, "success", count=len(result.workouts)))
    return results


def enabled_clients(settings: Settings) -> list[ClientEntry]:
    entries: list[ClientEntry] = []
    if settings.oura_pat:
        entries.append(ClientEntry("oura", OuraClient(settings.oura_pat), ""))
    else:
        entries.append(ClientEntry("oura", None, "OURA_PERSONAL_ACCESS_TOKEN not set"))

    if (
        settings.whoop_client_id
        and settings.whoop_client_secret
        and settings.whoop_refresh_token
    ):
        entries.append(
            ClientEntry(
                "whoop",
                WhoopClient(
                    client_id=settings.whoop_client_id,
                    client_secret=settings.whoop_client_secret,
                    refresh_token=settings.whoop_refresh_token,
                    env_path=settings.env_path,
                    local_timezone=settings.local_timezone,
                ),
                "",
            )
        )
    elif settings.whoop_client_id and settings.whoop_client_secret:
        entries.append(
            ClientEntry("whoop", None, "WHOOP_REFRESH_TOKEN missing — run `wearable auth whoop`")
        )
    else:
        entries.append(ClientEntry("whoop", None, "WHOOP client id/secret not set"))

    if (
        settings.google_client_id
        and settings.google_client_secret
        and settings.google_refresh_token
    ):
        entries.append(
            ClientEntry(
                "google_health",
                GoogleHealthClient(
                    client_id=settings.google_client_id,
                    client_secret=settings.google_client_secret,
                    refresh_token=settings.google_refresh_token,
                    env_path=settings.env_path,
                    local_timezone=settings.local_timezone,
                ),
                "",
            )
        )
    elif settings.google_client_id and settings.google_client_secret:
        entries.append(
            ClientEntry(
                "google_health",
                None,
                "GOOGLE_HEALTH_REFRESH_TOKEN missing — run `wearable auth google`",
            )
        )
    else:
        entries.append(
            ClientEntry("google_health", None, "GOOGLE_HEALTH client id/secret not set")
        )
    return entries


def _call_with_retry(
    call: Callable,
    device: str,
    *,
    max_attempts: int = 3,
    base_backoff: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
):
    """Generic retry helper: call ``call()`` with bounded retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in _RETRYABLE_STATUS:
                raise
            wait = _retry_after_or_backoff(exc.response, attempt, base_backoff)
            last_exc = exc
            log.warning(
                "%s: HTTP %s (attempt %d/%d) — retrying in %.1fs",
                device, status, attempt, max_attempts, wait,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            wait = base_backoff * (2 ** (attempt - 1))
            last_exc = exc
            log.warning(
                "%s: %s (attempt %d/%d) — retrying in %.1fs",
                device, type(exc).__name__, attempt, max_attempts, wait,
            )
        if attempt < max_attempts:
            sleep(wait)
    assert last_exc is not None
    raise last_exc


def fetch_with_retry(
    client: _Fetcher,
    day: date,
    *,
    max_attempts: int = 3,
    base_backoff: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """Call ``client.fetch_day(day)`` with bounded retry on transient errors."""
    return _call_with_retry(
        lambda: client.fetch_day(day),
        client.device,
        max_attempts=max_attempts,
        base_backoff=base_backoff,
        sleep=sleep,
    )


def _retry_after_or_backoff(
    response: httpx.Response, attempt: int, base_backoff: float
) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass
    return base_backoff * (2 ** (attempt - 1))


def pull_one_day(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    *,
    clients: list[ClientEntry] | None = None,
) -> list[DeviceResult]:
    entries = clients if clients is not None else enabled_clients(settings)
    results: list[DeviceResult] = []
    for entry in entries:
        if entry.client is None:
            log.info("%s (%s): skipped — %s", entry.device, day, entry.reason)
            results.append(
                DeviceResult(device=entry.device, status="skipped", error=entry.reason)
            )
            continue
        try:
            result = fetch_with_retry(entry.client, day)
        except Exception as exc:
            log.warning("%s (%s): failed — %s: %s", entry.device, day, type(exc).__name__, exc)
            results.append(
                DeviceResult(device=entry.device, status="failed", error=str(exc))
            )
            continue
        with conn:
            for raw in result.raw:
                write_raw_payload(
                    conn,
                    device=entry.device,
                    endpoint=raw.endpoint,
                    date=raw.date,
                    payload=raw.payload,
                )
            upsert_daily_metrics(conn, result.metrics)
        log.info("%s (%s): ok", entry.device, day)
        results.append(
            DeviceResult(
                device=entry.device,
                status="success",
                metrics=result.metrics,
            )
        )
    return results


def backfill(
    conn: sqlite3.Connection,
    settings: Settings,
    since: date,
    until: date | None = None,
    *,
    skip_existing: bool = False,
) -> list[tuple[date, list[DeviceResult]]]:
    """Walk days from ``since`` through ``until`` (default: yesterday)."""
    until = until or (date.today() - timedelta(days=1))
    out: list[tuple[date, list[DeviceResult]]] = []
    day = since
    entries = enabled_clients(settings)  # cached across days
    while day <= until:
        if skip_existing and _has_metrics_for_all(conn, day, entries):
            log.info("%s: skipping — all configured devices already have rows", day)
            out.append((day, []))
        else:
            out.append((day, pull_one_day(conn, settings, day, clients=entries)))
        day += timedelta(days=1)
    return out


def _has_metrics_for_all(
    conn: sqlite3.Connection, day: date, entries: list[ClientEntry]
) -> bool:
    active = {e.device for e in entries if e.client is not None}
    if not active:
        return False
    rows = conn.execute(
        "SELECT device FROM daily_metrics WHERE date = ?", (day.isoformat(),)
    ).fetchall()
    present = {row[0] for row in rows}
    return active.issubset(present)


def summarize(results: list[DeviceResult]) -> tuple[int, int, int]:
    """Return (success_count, failed_count, skipped_count)."""
    s = sum(1 for r in results if r.status == "success")
    f = sum(1 for r in results if r.status == "failed")
    k = sum(1 for r in results if r.status == "skipped")
    return s, f, k


def exit_code_for(results: list[DeviceResult]) -> int:
    """0 = all configured devices ok; 2 = partial failure; 1 = all failed/none configured."""
    s, f, _k = summarize(results)
    if s == 0:
        return 1
    if f > 0:
        return 2
    return 0
