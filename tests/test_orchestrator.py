from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from wearable_pipeline import db
from wearable_pipeline.config import Settings
from wearable_pipeline.models import DailyMetrics, FetchResult, RawPayload
from wearable_pipeline.orchestrator import (
    ClientEntry,
    DeviceResult,
    backfill,
    exit_code_for,
    fetch_with_retry,
    pull_one_day,
    summarize,
)


# --- fake clients ------------------------------------------------------------


class _StubClient:
    def __init__(self, device: str, behavior):
        self.device = device
        self._behavior = behavior
        self.calls: list[date] = []

    def fetch_day(self, day):
        self.calls.append(day)
        return self._behavior(day, len(self.calls))


def _ok_result(day: date, device: str) -> FetchResult:
    return FetchResult(
        metrics=DailyMetrics(date=day, device=device, sleep_score=80),
        raw=[RawPayload(endpoint="stub", date=day.isoformat(), payload={"k": "v"})],
    )


def _raise_status(status: int, retry_after: str | None = None):
    def _fn(day, _attempt):
        req = httpx.Request("GET", "https://example.com/x")
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        resp = httpx.Response(status, request=req, json={"detail": "x"}, headers=headers)
        raise httpx.HTTPStatusError("boom", request=req, response=resp)
    return _fn


# --- fetch_with_retry --------------------------------------------------------


def test_fetch_with_retry_no_retry_on_success() -> None:
    client = _StubClient("oura", lambda d, _a: _ok_result(d, "oura"))
    sleeps: list[float] = []
    result = fetch_with_retry(client, date(2026, 6, 12), sleep=sleeps.append)
    assert result.metrics.device == "oura"
    assert len(client.calls) == 1
    assert sleeps == []


def test_fetch_with_retry_retries_on_500_then_succeeds() -> None:
    def behavior(day, attempt):
        if attempt == 1:
            raise httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "https://example.com/x"),
                response=httpx.Response(500, request=httpx.Request("GET", "https://example.com/x"), json={}),
            )
        return _ok_result(day, "oura")

    client = _StubClient("oura", behavior)
    sleeps: list[float] = []
    result = fetch_with_retry(client, date(2026, 6, 12), sleep=sleeps.append, base_backoff=0.5)
    assert result.metrics.device == "oura"
    assert len(client.calls) == 2
    assert sleeps == [0.5]


def test_fetch_with_retry_honors_retry_after_on_429() -> None:
    def behavior(day, attempt):
        if attempt == 1:
            raise httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "https://example.com/x"),
                response=httpx.Response(
                    429,
                    request=httpx.Request("GET", "https://example.com/x"),
                    json={},
                    headers={"Retry-After": "7"},
                ),
            )
        return _ok_result(day, "oura")

    client = _StubClient("oura", behavior)
    sleeps: list[float] = []
    fetch_with_retry(client, date(2026, 6, 12), sleep=sleeps.append)
    assert sleeps == [7.0]


def test_fetch_with_retry_does_not_retry_on_401() -> None:
    client = _StubClient("oura", _raise_status(401))
    sleeps: list[float] = []
    with pytest.raises(httpx.HTTPStatusError):
        fetch_with_retry(client, date(2026, 6, 12), sleep=sleeps.append)
    assert len(client.calls) == 1  # no retry
    assert sleeps == []


def test_fetch_with_retry_does_not_retry_on_403() -> None:
    client = _StubClient("oura", _raise_status(403))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_with_retry(client, date(2026, 6, 12), sleep=lambda _: None)
    assert len(client.calls) == 1


def test_fetch_with_retry_retries_on_network_error() -> None:
    def behavior(day, attempt):
        if attempt < 3:
            raise httpx.ConnectError("conn refused")
        return _ok_result(day, "oura")

    client = _StubClient("oura", behavior)
    sleeps: list[float] = []
    fetch_with_retry(client, date(2026, 6, 12), sleep=sleeps.append, base_backoff=0.5)
    assert len(client.calls) == 3
    # base * 2^(attempt-1): 0.5, 1.0
    assert sleeps == [0.5, 1.0]


def test_fetch_with_retry_gives_up_after_max_attempts() -> None:
    client = _StubClient("oura", _raise_status(503))
    sleeps: list[float] = []
    with pytest.raises(httpx.HTTPStatusError) as exc:
        fetch_with_retry(
            client, date(2026, 6, 12), max_attempts=3, sleep=sleeps.append
        )
    assert exc.value.response.status_code == 503
    assert len(client.calls) == 3
    assert len(sleeps) == 2  # waits between attempts 1->2 and 2->3, not after the last


# --- pull_one_day with stubbed entries ---------------------------------------


@pytest.fixture
def conn_db(tmp_path: Path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


def _settings_stub(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "ignored.db",
        env_path=tmp_path / ".env",
        local_timezone="America/Los_Angeles",
        oura_pat=None,
        whoop_client_id=None,
        whoop_client_secret=None,
        whoop_redirect_uri=None,
        whoop_refresh_token=None,
        google_client_id=None,
        google_client_secret=None,
        google_redirect_uri=None,
        google_refresh_token=None,
        whoop_ble_address=None,
        fitbit_ble_address=None,
    )


def test_pull_one_day_records_success_and_failure(conn_db, tmp_path: Path) -> None:
    day = date(2026, 6, 12)
    good = _StubClient("oura", lambda d, _: _ok_result(d, "oura"))
    bad = _StubClient("whoop", _raise_status(500))
    entries = [
        ClientEntry("oura", good, ""),
        ClientEntry("whoop", bad, ""),
        ClientEntry("google_health", None, "skipped for test"),
    ]
    results = pull_one_day(conn_db, _settings_stub(tmp_path), day, clients=entries)

    by_device = {r.device: r for r in results}
    assert by_device["oura"].status == "success"
    assert by_device["whoop"].status == "failed"
    assert by_device["google_health"].status == "skipped"

    # Successful device's row landed in the DB; failed one's didn't.
    rows = {row["device"] for row in conn_db.execute("SELECT device FROM daily_metrics")}
    assert rows == {"oura"}
    raw_devices = {
        row["device"] for row in conn_db.execute("SELECT device FROM raw_payloads")
    }
    assert raw_devices == {"oura"}


def test_pull_one_day_calls_skipped_entries_without_invoking_clients(conn_db, tmp_path) -> None:
    entries = [ClientEntry("oura", None, "no PAT")]
    results = pull_one_day(conn_db, _settings_stub(tmp_path), date(2026, 6, 12), clients=entries)
    assert results == [DeviceResult(device="oura", status="skipped", error="no PAT")]


# --- summarize / exit_code_for ----------------------------------------------


def test_summarize_counts() -> None:
    rs = [
        DeviceResult("a", "success"),
        DeviceResult("b", "failed", error="x"),
        DeviceResult("c", "skipped", error="y"),
        DeviceResult("d", "success"),
    ]
    assert summarize(rs) == (2, 1, 1)


@pytest.mark.parametrize(
    "results,expected",
    [
        ([DeviceResult("a", "success")], 0),
        (
            [DeviceResult("a", "success"), DeviceResult("b", "failed", error="x")],
            2,
        ),
        ([DeviceResult("a", "failed", error="x")], 1),
        ([DeviceResult("a", "skipped", error="y")], 1),
    ],
)
def test_exit_code_for(results, expected) -> None:
    assert exit_code_for(results) == expected


# --- backfill ---------------------------------------------------------------


def test_backfill_walks_inclusive_range(conn_db, tmp_path) -> None:
    """Verify backfill calls pull_one_day for each day in the range."""
    calls: list[date] = []

    def stub_pull(conn, settings, day, *, clients=None):
        calls.append(day)
        return []

    from wearable_pipeline import orchestrator

    monkey = MagicMock(side_effect=stub_pull)
    original = orchestrator.pull_one_day
    orchestrator.pull_one_day = monkey
    try:
        backfill(
            conn_db,
            _settings_stub(tmp_path),
            since=date(2026, 6, 10),
            until=date(2026, 6, 13),
        )
    finally:
        orchestrator.pull_one_day = original

    assert calls == [
        date(2026, 6, 10),
        date(2026, 6, 11),
        date(2026, 6, 12),
        date(2026, 6, 13),
    ]


def test_pull_workouts_persists_and_reports(tmp_path):
    from datetime import date, datetime, timezone
    import wearable_pipeline.db as db
    from wearable_pipeline.models import Workout, WorkoutFetchResult, RawPayload
    from wearable_pipeline.orchestrator import ClientEntry, pull_workouts

    conn = db.connect(tmp_path / "w.db")
    db.migrate(conn)

    class FakeWhoop:
        device = "whoop"
        def fetch_workouts(self, since, until):
            w = Workout(
                device="whoop", provider_id="w1", sport="running",
                start_time=datetime(2026, 6, 24, 20, tzinfo=timezone.utc),
                end_time=datetime(2026, 6, 24, 21, tzinfo=timezone.utc),
                duration_minutes=60, avg_hr=145.0, max_hr=178.0, calories=478,
                date=date(2026, 6, 24),
            )
            raw = [RawPayload(endpoint="v2/activity/workout", date="r", payload={"records": []})]
            return WorkoutFetchResult(workouts=[w], raw=raw)

    entries = [
        ClientEntry("whoop", FakeWhoop(), ""),
        ClientEntry("oura", object(), ""),  # must be skipped, never called
    ]
    results = pull_workouts(
        conn, settings=None, since=date(2026, 6, 24), until=date(2026, 6, 24),
        clients=entries,
    )
    by_device = {r.device: r for r in results}
    assert "oura" not in by_device  # Oura excluded entirely
    assert by_device["whoop"].status == "success"
    assert by_device["whoop"].count == 1
    n = conn.execute("SELECT COUNT(*) FROM workouts WHERE device='whoop'").fetchone()[0]
    assert n == 1
    raw_n = conn.execute("SELECT COUNT(*) FROM raw_payloads WHERE device='whoop'").fetchone()[0]
    assert raw_n == 1


def test_backfill_skip_existing_skips_complete_days(conn_db, tmp_path) -> None:
    settings = _settings_stub(tmp_path)
    # Pre-populate a 'success' row for one device so the "skip-existing"
    # logic recognizes the day as complete (only one device active in stub).
    today = date(2026, 6, 12)
    yesterday = today - timedelta(days=1)

    # Use a stub that has a single active client; if all active devices have
    # rows for the day, skip_existing skips the pull.
    good_client = _StubClient("oura", lambda d, _: _ok_result(d, "oura"))

    from wearable_pipeline.storage import upsert_daily_metrics
    upsert_daily_metrics(
        conn_db,
        DailyMetrics(date=yesterday, device="oura", sleep_score=70),
    )

    from wearable_pipeline import orchestrator

    original_enabled = orchestrator.enabled_clients
    orchestrator.enabled_clients = lambda _s: [ClientEntry("oura", good_client, "")]
    try:
        walks = backfill(
            conn_db,
            settings,
            since=yesterday,
            until=yesterday,
            skip_existing=True,
        )
    finally:
        orchestrator.enabled_clients = original_enabled

    # Day was skipped — no new fetch call on the stub client.
    assert good_client.calls == []
    assert walks == [(yesterday, [])]
