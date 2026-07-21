from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Device = Literal["oura", "whoop", "google_health"]


class DailyMetrics(BaseModel):
    """Normalized daily metrics for one device on one date.

    The *_score fields use each device's proprietary 0..100 scale and are NOT
    comparable across devices. For cross-device comparison use rank correlation
    on raw values (e.g. Spearman on readiness_score per-device), never averages.
    """

    date: date
    device: Device

    total_sleep_minutes: int | None = None
    sleep_efficiency: float | None = None  # 0..1
    sleep_latency_minutes: int | None = None
    rem_minutes: int | None = None
    deep_minutes: int | None = None
    light_minutes: int | None = None
    awake_minutes: int | None = None
    sleep_score: int | None = Field(default=None, ge=0, le=100)

    readiness_score: int | None = Field(default=None, ge=0, le=100)
    hrv_ms: float | None = None
    resting_hr: float | None = None
    respiratory_rate: float | None = None
    body_temp_deviation: float | None = None  # Oura
    skin_temp: float | None = None  # Whoop

    strain_or_activity_score: float | None = None
    active_calories: int | None = None
    steps: int | None = None


@dataclass(frozen=True)
class RawPayload:
    """One captured API response, archived before normalization."""

    endpoint: str
    date: str | None
    payload: Any


@dataclass(frozen=True)
class FetchResult:
    """What every WearableClient.fetch_day returns.

    Clients stay storage-agnostic — the orchestrator/CLI persists ``raw``
    rows first, then upserts ``metrics``, inside a single transaction.
    """

    metrics: DailyMetrics
    raw: list[RawPayload]


class Workout(BaseModel):
    """One workout session as a single device measured it.

    Stored many-per-day in the `workouts` table, keyed by (device, provider_id).
    Fields a device does not expose stay None — never zero-filled.
    """

    device: str
    provider_id: str
    sport: str | None = None
    start_time: datetime
    end_time: datetime | None = None
    duration_minutes: int | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    calories: int | None = None
    date: date  # local-tz date of start_time


@dataclass(frozen=True)
class WorkoutFetchResult:
    """What WearableClient.fetch_workouts returns. Storage-agnostic, like FetchResult."""

    workouts: list[Workout]
    raw: list[RawPayload]
