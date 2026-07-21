from __future__ import annotations

from datetime import date
from typing import Protocol

from ..models import FetchResult


class WearableClient(Protocol):
    """Common interface for all device clients.

    Implementations return a ``FetchResult`` carrying the normalized
    ``DailyMetrics`` plus the list of raw API responses observed during the
    fetch. The caller (orchestrator / CLI) is responsible for persisting
    ``result.raw`` to ``raw_payloads`` BEFORE upserting ``result.metrics``
    into ``daily_metrics`` — that ordering guarantees source data survives
    even if the normalization is wrong.

    Fields the device does not expose should remain ``None`` in
    ``DailyMetrics`` — do not substitute zeros or device defaults.
    """

    device: str  # "oura" | "whoop" | "google_health"

    def fetch_day(self, day: date) -> FetchResult:
        """Fetch and normalize one day of data for this device."""
        ...
