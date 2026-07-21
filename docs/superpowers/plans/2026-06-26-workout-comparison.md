# Workout Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull recent Whoop and Google Health workout sessions into a local `workouts` table and add a Streamlit section comparing avg HR, max HR, and calories for the same workout across the two devices.

**Architecture:** A new `workouts` table (many rows per day, keyed by `device`+`provider_id`) sits beside `daily_metrics`. Each device client gains a storage-agnostic `fetch_workouts(since, until)` returning normalized `Workout`s plus raw payloads (raw-first rule). An opt-in `--workouts` flag on `wearable pull` drives a new orchestrator path for Whoop + Google only. "Same workout" matching across devices is a pure time-overlap function computed at view time in the viz.

**Tech Stack:** Python 3.11+, Pydantic v2, httpx, Typer, SQLite, pytest + respx, Streamlit + pandas.

## Global Constraints

- Clients import no sqlite; they return `(workouts, raw)` and the caller persists. (architecture rule)
- Raw payloads are written to `raw_payloads` **before** upserting workouts, in one transaction per device. (raw-first rule)
- Dates/timestamps are ISO-8601 TEXT; timestamps in UTC. No SQLite-specific types, no reliance on ROWID. (SQLite-now/Postgres-later)
- Fields a device does not expose stay `None`; never substitute zeros. (comparison-signal rule)
- Migrations are additive: add a new `migrations/NNNN_*.sql`; never edit a prior migration. (db.migrate rule)
- Never average `*_score`/values across devices; the comparison shows side-by-side values + deltas only.
- Whoop refresh tokens rotate — never call the Whoop token endpoint directly with `httpx.post`; go through `TokenManager`.
- Google Health: source-filter to `dataSource.platform == "FITBIT"`; never call `dataPoints:reconcile`.
- This repo is **not** a git repository. Skip all `git add`/`git commit` steps; treat "Commit" steps as "checkpoint: re-run the full suite with `uv run pytest` and confirm green."

---

### Task 1: `workouts` table migration

**Files:**
- Create: `migrations/0004_workouts.sql`
- Test: `tests/test_db.py` (add one test)

**Interfaces:**
- Consumes: `db.connect`, `db.migrate` (existing).
- Produces: a `workouts` table with columns `device, provider_id, sport, start_time, end_time, duration_minutes, avg_hr, max_hr, calories, date, fetched_at` and `PRIMARY KEY (device, provider_id)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_db.py`:

```python
def test_migrate_creates_workouts_table(tmp_path):
    import wearable_pipeline.db as db
    conn = db.connect(tmp_path / "w.db")
    db.migrate(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workouts)").fetchall()}
    assert cols == {
        "device", "provider_id", "sport", "start_time", "end_time",
        "duration_minutes", "avg_hr", "max_hr", "calories", "date", "fetched_at",
    }
    pk = [row[1] for row in conn.execute("PRAGMA table_info(workouts)").fetchall() if row[5]]
    assert set(pk) == {"device", "provider_id"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py::test_migrate_creates_workouts_table -v`
Expected: FAIL — `no such table: workouts`.

- [ ] **Step 3: Write the migration**

Create `migrations/0004_workouts.sql`:

```sql
CREATE TABLE workouts (
    device           TEXT NOT NULL,      -- 'whoop' | 'google_health'
    provider_id      TEXT NOT NULL,      -- the device's own workout id
    sport            TEXT,               -- labelled sport (run, strength, …)
    start_time       TEXT NOT NULL,      -- ISO-8601 UTC
    end_time         TEXT,               -- ISO-8601 UTC
    duration_minutes INTEGER,
    avg_hr           REAL,
    max_hr           REAL,
    calories         INTEGER,            -- each device's own active-cal definition
    date             TEXT NOT NULL,      -- local-tz date of start_time (for filtering)
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (device, provider_id)
);
CREATE INDEX idx_workouts_date ON workouts (date);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db.py::test_migrate_creates_workouts_table -v`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `uv run pytest -q`. Confirm green.

---

### Task 2: `Workout` model, `WorkoutFetchResult`, and `upsert_workout`

**Files:**
- Modify: `src/wearable_pipeline/models.py`
- Modify: `src/wearable_pipeline/storage.py`
- Test: `tests/test_storage.py` (add tests)

**Interfaces:**
- Consumes: existing `RawPayload`, `Device`, `write_raw_payload`.
- Produces:
  - `models.Workout(BaseModel)` with fields: `device: str`, `provider_id: str`, `sport: str | None = None`, `start_time: datetime`, `end_time: datetime | None = None`, `duration_minutes: int | None = None`, `avg_hr: float | None = None`, `max_hr: float | None = None`, `calories: int | None = None`, `date: date`.
  - `models.WorkoutFetchResult` dataclass: `workouts: list[Workout]`, `raw: list[RawPayload]`.
  - `storage.upsert_workout(conn, workout: Workout) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_storage.py`:

```python
from datetime import date, datetime, timezone

def test_upsert_workout_inserts_then_updates(tmp_path):
    import wearable_pipeline.db as db
    from wearable_pipeline.models import Workout
    from wearable_pipeline.storage import upsert_workout

    conn = db.connect(tmp_path / "w.db")
    db.migrate(conn)

    w = Workout(
        device="whoop", provider_id="abc", sport="running",
        start_time=datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 24, 21, 0, tzinfo=timezone.utc),
        duration_minutes=60, avg_hr=145.0, max_hr=178.0, calories=478,
        date=date(2026, 6, 24),
    )
    upsert_workout(conn, w)
    row = conn.execute(
        "SELECT sport, avg_hr, max_hr, calories FROM workouts WHERE device=? AND provider_id=?",
        ("whoop", "abc"),
    ).fetchone()
    assert row == ("running", 145.0, 178.0, 478)

    # Re-upsert same key with changed values → update in place, still one row.
    upsert_workout(conn, w.model_copy(update={"avg_hr": 150.0, "calories": 500}))
    rows = conn.execute("SELECT avg_hr, calories FROM workouts").fetchall()
    assert rows == [(150.0, 500)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py::test_upsert_workout_inserts_then_updates -v`
Expected: FAIL — `ImportError: cannot import name 'Workout'`.

- [ ] **Step 3a: Add the model**

In `src/wearable_pipeline/models.py`, add `datetime` to the `datetime` import line (`from datetime import date, datetime`) and append:

```python
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
```

- [ ] **Step 3b: Add the storage function**

In `src/wearable_pipeline/storage.py`, add `Workout` to the models import (`from .models import DailyMetrics, Workout`) and append:

```python
def upsert_workout(conn: sqlite3.Connection, workout: Workout) -> None:
    payload = workout.model_dump()
    payload["start_time"] = workout.start_time.isoformat()
    payload["end_time"] = workout.end_time.isoformat() if workout.end_time else None
    payload["date"] = workout.date.isoformat()
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()

    cols = list(payload.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in cols if c not in ("device", "provider_id")
    )
    conn.execute(
        f"INSERT INTO workouts ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(device, provider_id) DO UPDATE SET {updates}",
        [payload[c] for c in cols],
    )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py::test_upsert_workout_inserts_then_updates -v`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `uv run pytest -q`. Confirm green.

---

### Task 3: `WhoopClient.fetch_workouts`

**Files:**
- Modify: `src/wearable_pipeline/clients/whoop.py`
- Test: `tests/test_whoop_client.py` (add test)

**Interfaces:**
- Consumes: existing `WhoopClient.__init__`, `_open_client`, `_authed_get`, `_utc_window`, `_iso_utc`, `_kj_to_kcal`, `TokenManager`.
- Produces: `WhoopClient.fetch_workouts(self, since: date, until: date) -> WorkoutFetchResult`.

**Whoop v2 workout shape (for the fixture):** `GET /developer/v2/activity/workout?start=…&end=…&limit=25` → `{"records": [{"id": "...", "start": "...Z", "end": "...Z", "sport_name": "running", "score": {"average_heart_rate": 145, "max_heart_rate": 178, "kilojoule": 2000.0}}], "next_token": null}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_whoop_client.py` (follow the existing respx fixture style in that file for auth/token setup; reuse its helper for building a `WhoopClient` with an injected `http_client` and a stub token):

```python
def test_fetch_workouts_maps_fields(respx_mock, whoop_client_factory):
    # whoop_client_factory builds a WhoopClient with an injected httpx.Client and
    # a TokenManager whose access token is pre-stubbed (mirror existing tests).
    import respx, httpx
    from datetime import date

    body = {
        "records": [
            {
                "id": "w1",
                "start": "2026-06-24T20:00:00.000Z",
                "end": "2026-06-24T21:00:00.000Z",
                "sport_name": "running",
                "score": {
                    "average_heart_rate": 145,
                    "max_heart_rate": 178,
                    "kilojoule": 2000.0,
                },
            }
        ],
        "next_token": None,
    }
    respx_mock.get(url__regex=r".*/developer/v2/activity/workout.*").mock(
        return_value=httpx.Response(200, json=body)
    )

    client = whoop_client_factory()
    result = client.fetch_workouts(date(2026, 6, 24), date(2026, 6, 24))

    assert len(result.workouts) == 1
    w = result.workouts[0]
    assert w.device == "whoop"
    assert w.provider_id == "w1"
    assert w.sport == "running"
    assert w.avg_hr == 145
    assert w.max_hr == 178
    assert w.calories == round(2000.0 / 4.184)  # 478
    assert w.duration_minutes == 60
    # local-tz date of start (America/Los_Angeles → 2026-06-24 13:00 local)
    assert w.date.isoformat() == "2026-06-24"
    assert any("workout" in r.endpoint for r in result.raw)
```

> If `tests/test_whoop_client.py` has no reusable `whoop_client_factory`/fixture, inline the client construction exactly as the existing tests in that file do (same `http_client`/token stub), and adjust the test to use it. Do not change the existing tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_whoop_client.py::test_fetch_workouts_maps_fields -v`
Expected: FAIL — `AttributeError: 'WhoopClient' object has no attribute 'fetch_workouts'`.

- [ ] **Step 3: Implement `fetch_workouts`**

In `src/wearable_pipeline/clients/whoop.py`:
- Add `WORKOUT_ENDPOINT = "v2/activity/workout"` next to the other endpoint constants.
- Add `WorkoutFetchResult, Workout` to the models import: `from ..models import DailyMetrics, FetchResult, RawPayload, Workout, WorkoutFetchResult`.
- Add the method:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_whoop_client.py::test_fetch_workouts_maps_fields -v`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `uv run pytest -q`. Confirm green.

---

### Task 4: `match_workouts` pure function

**Files:**
- Create: `src/wearable_pipeline/workouts.py`
- Test: `tests/test_workouts_match.py`

**Interfaces:**
- Consumes: `models.Workout`.
- Produces:
  - `workouts.WorkoutPair` dataclass: `whoop: Workout`, `google: Workout`, `overlap_minutes: float`.
  - `workouts.MatchResult` dataclass: `pairs: list[WorkoutPair]`, `unmatched_whoop: list[Workout]`, `unmatched_google: list[Workout]`.
  - `workouts.match_workouts(whoop: list[Workout], google: list[Workout]) -> MatchResult`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workouts_match.py`:

```python
from datetime import date, datetime, timezone

from wearable_pipeline.models import Workout
from wearable_pipeline.workouts import match_workouts


def _w(device, pid, start_h, end_h):
    return Workout(
        device=device, provider_id=pid,
        start_time=datetime(2026, 6, 24, start_h, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 24, end_h, tzinfo=timezone.utc),
        date=date(2026, 6, 24),
    )


def test_overlapping_sessions_pair():
    whoop = [_w("whoop", "a", 20, 21)]
    google = [_w("google_health", "x", 20, 21)]
    res = match_workouts(whoop, google)
    assert len(res.pairs) == 1
    assert res.pairs[0].whoop.provider_id == "a"
    assert res.pairs[0].google.provider_id == "x"
    assert not res.unmatched_whoop and not res.unmatched_google


def test_disjoint_sessions_stay_unmatched():
    whoop = [_w("whoop", "a", 6, 7)]
    google = [_w("google_health", "x", 20, 21)]
    res = match_workouts(whoop, google)
    assert res.pairs == []
    assert [w.provider_id for w in res.unmatched_whoop] == ["a"]
    assert [g.provider_id for g in res.unmatched_google] == ["x"]


def test_each_session_matched_at_most_once():
    # One whoop session overlaps two google sessions; only the best-overlap wins.
    whoop = [_w("whoop", "a", 20, 22)]
    google = [_w("google_health", "x", 20, 22), _w("google_health", "y", 21, 22)]
    res = match_workouts(whoop, google)
    assert len(res.pairs) == 1
    assert res.pairs[0].google.provider_id == "x"  # larger overlap (2h vs 1h)
    assert [g.provider_id for g in res.unmatched_google] == ["y"]


def test_empty_inputs():
    res = match_workouts([], [])
    assert res.pairs == [] and res.unmatched_whoop == [] and res.unmatched_google == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workouts_match.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wearable_pipeline.workouts'`.

- [ ] **Step 3: Implement `match_workouts`**

Create `src/wearable_pipeline/workouts.py`:

```python
"""Match the same real-world workout across devices by time overlap.

Whoop and Google Health assign different ids to the same session, so we pair
them when their [start, end] windows overlap. Greedy by largest overlap; each
session is matched at most once. Unmatched sessions are returned, never dropped.
Pure function — no DB, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Workout


@dataclass(frozen=True)
class WorkoutPair:
    whoop: Workout
    google: Workout
    overlap_minutes: float


@dataclass(frozen=True)
class MatchResult:
    pairs: list[WorkoutPair]
    unmatched_whoop: list[Workout]
    unmatched_google: list[Workout]


def _end(w: Workout) -> datetime:
    return w.end_time or w.start_time


def _overlap_minutes(a: Workout, b: Workout) -> float:
    start = max(a.start_time, b.start_time)
    end = min(_end(a), _end(b))
    delta = (end - start).total_seconds() / 60
    return delta if delta > 0 else 0.0


def match_workouts(
    whoop: list[Workout], google: list[Workout]
) -> MatchResult:
    candidates: list[tuple[float, int, int]] = []
    for i, w in enumerate(whoop):
        for j, g in enumerate(google):
            ov = _overlap_minutes(w, g)
            if ov > 0:
                candidates.append((ov, i, j))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_w: set[int] = set()
    used_g: set[int] = set()
    pairs: list[WorkoutPair] = []
    for ov, i, j in candidates:
        if i in used_w or j in used_g:
            continue
        used_w.add(i)
        used_g.add(j)
        pairs.append(WorkoutPair(whoop=whoop[i], google=google[j], overlap_minutes=ov))

    unmatched_whoop = [w for i, w in enumerate(whoop) if i not in used_w]
    unmatched_google = [g for j, g in enumerate(google) if j not in used_g]
    return MatchResult(
        pairs=pairs,
        unmatched_whoop=unmatched_whoop,
        unmatched_google=unmatched_google,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_workouts_match.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Checkpoint**

Run: `uv run pytest -q`. Confirm green.

---

### Task 5: `GoogleHealthClient.fetch_workouts` (live-probe gated)

**Files:**
- Modify: `src/wearable_pipeline/clients/google_health.py`
- Test: `tests/test_google_health_client.py` (add test, after probing)

**Interfaces:**
- Consumes: existing `GoogleHealthClient.__init__`, `_open_client`, `_authed_request`, `_is_fitbit_source`, `TokenManager`, `_to_float`, `_int_or_none`.
- Produces: `GoogleHealthClient.fetch_workouts(self, since: date, until: date) -> WorkoutFetchResult` with the same signature/return type as the Whoop method.

> **This task carries the design's open risk.** Probe the live v4 API first. If Google Health does not expose per-session Fitbit workouts to a third-party OAuth client, STOP, write the finding into `CLAUDE.md` (the "Google Health quirks" section) the same way the readiness/sleep-score dead-end is documented, and report back to the owner. Do not fabricate a fixture or fall back to the reconciled stream.

- [ ] **Step 1: Probe the live API**

With `.env` credentials present, find the workout/exercise/session data type id. Try the discovery doc and candidate list endpoints (DO NOT pass a server-side `filter` param — Google rejects them; page unfiltered and filter client-side):

```bash
uv run python - <<'PY'
from datetime import date, timedelta
from pathlib import Path
import httpx
from wearable_pipeline.config import load_settings
from wearable_pipeline.clients._oauth import TokenManager

s = load_settings()
tm = TokenManager(
    client_id=s.google_client_id, client_secret=s.google_client_secret,
    refresh_token=s.google_refresh_token,
    token_endpoint="https://oauth2.googleapis.com/token",
    env_key="GOOGLE_HEALTH_REFRESH_TOKEN", env_path=s.env_path,
)
tok = tm.get_access_token()
h = {"Authorization": f"Bearer {tok}"}
base = "https://health.googleapis.com/v4/users/me/dataTypes"
for dt in ("activity", "activity-segment", "exercise", "workout",
           "activity-session", "session", "physical-activity"):
    try:
        r = httpx.get(f"{base}/{dt}/dataPoints", params={"pageSize": 5}, headers=h, timeout=30)
        print(dt, r.status_code, r.text[:300])
    except Exception as e:
        print(dt, "ERR", e)
PY
```

Record which id returns 200 with Fitbit-sourced session data and the JSON shape (where `start`/`end`, sport label, and avg/max HR live). If none do, STOP per the gate above.

- [ ] **Step 2: Write the failing test from the observed shape**

Add to `tests/test_google_health_client.py`, mirroring the existing respx fixture style in that file. Use the **actual** field names observed in Step 1; the skeleton below assumes a `dataPoints` list with a session payload and `dataSource.platform == "FITBIT"` — adjust keys to match reality:

```python
def test_fetch_workouts_maps_fitbit_session(respx_mock, google_client_factory):
    import httpx
    from datetime import date
    body = {
        "dataPoints": [
            {
                "dataSource": {"platform": "FITBIT"},
                # >>> replace the inner keys with the real shape from Step 1 <<<
                "<sessionKey>": {
                    "id": "g1",
                    "activityType": "running",
                    "interval": {
                        "startTime": "2026-06-24T20:00:00Z",
                        "endTime": "2026-06-24T21:00:00Z",
                    },
                    "averageHeartRate": 142,
                    "maxHeartRate": 175,
                    "calories": 455,
                },
            },
            {"dataSource": {"platform": "OTHER"}, "<sessionKey>": {"id": "skip"}},
        ]
    }
    respx_mock.get(url__regex=r".*/dataTypes/<typeId>/dataPoints.*").mock(
        return_value=httpx.Response(200, json=body)
    )
    client = google_client_factory()
    result = client.fetch_workouts(date(2026, 6, 24), date(2026, 6, 24))
    ids = [w.provider_id for w in result.workouts]
    assert ids == ["g1"]  # non-Fitbit source filtered out
    w = result.workouts[0]
    assert w.device == "google_health"
    assert w.avg_hr == 142 and w.max_hr == 175 and w.calories == 455
    assert w.duration_minutes == 60
    assert w.date.isoformat() == "2026-06-24"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_google_health_client.py::test_fetch_workouts_maps_fitbit_session -v`
Expected: FAIL — `AttributeError: ... no attribute 'fetch_workouts'`.

- [ ] **Step 4: Implement `fetch_workouts`**

In `src/wearable_pipeline/clients/google_health.py`:
- Add `WorkoutFetchResult, Workout` to the models import.
- Add `TYPE_WORKOUT = "<typeId>"` (the id confirmed in Step 1) near the other type constants.
- Implement, filtering Fitbit-source client-side and filtering by local-tz date of start (reuse `_is_fitbit_source`; do NOT send a server-side `filter` param). Map provider_id, sport, start/end, avg/max HR, calories, duration, and `date` (local-tz of start). Use the real field names from Step 1. Append a `RawPayload(endpoint=_list_path(TYPE_WORKOUT), date="since..until", payload=body)` for each page. Skip records with no id/start (`continue`).

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_google_health_client.py::test_fetch_workouts_maps_fitbit_session -v`
Expected: PASS.

- [ ] **Step 6: Checkpoint**

Run: `uv run pytest -q`. Confirm green.

---

### Task 6: Orchestrator `pull_workouts` + CLI `--workouts` flag

**Files:**
- Modify: `src/wearable_pipeline/orchestrator.py`
- Modify: `src/wearable_pipeline/cli.py`
- Test: `tests/test_orchestrator.py` (add test)

**Interfaces:**
- Consumes: `enabled_clients`, `fetch_with_retry` (generalized below), `upsert_workout`, `write_raw_payload`, `Settings`.
- Produces:
  - `orchestrator.WorkoutResult` dataclass: `device: str`, `status: str`, `count: int = 0`, `error: str | None = None`.
  - `orchestrator.pull_workouts(conn, settings, since, until, *, clients=None) -> list[WorkoutResult]` — runs only `whoop` and `google_health` entries; Oura skipped.
  - CLI `pull` gains `--workouts` (bool, default False) and `--workout-days` (int, default 3).

> `fetch_with_retry` currently hardcodes `client.fetch_day`. Generalize it to take a callable: add a `call: Callable[[], FetchResult]`-style indirection, or add a sibling `_retry(callable, device, ...)`. Keep `fetch_with_retry` working for the existing daily path (do not break `tests/test_orchestrator.py`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_pull_workouts_persists_and_reports -v`
Expected: FAIL — `ImportError: cannot import name 'pull_workouts'`.

- [ ] **Step 3a: Generalize retry + add `pull_workouts`**

In `src/wearable_pipeline/orchestrator.py`:
- Add imports: `from .storage import upsert_daily_metrics, upsert_workout, write_raw_payload` and `from .models import DailyMetrics, FetchResult, Workout, WorkoutFetchResult`.
- Add a generic retry helper that reuses the existing backoff logic (refactor `fetch_with_retry` to delegate, so the daily path is unchanged):

```python
def _call_with_retry(call, device, *, max_attempts=3, base_backoff=2.0, sleep=time.sleep):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in _RETRYABLE_STATUS:
                raise
            wait = _retry_after_or_backoff(exc.response, attempt, base_backoff)
            last_exc = exc
            log.warning("%s: HTTP %s (attempt %d/%d) — retrying in %.1fs",
                        device, status, attempt, max_attempts, wait)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            wait = base_backoff * (2 ** (attempt - 1))
            last_exc = exc
            log.warning("%s: %s (attempt %d/%d) — retrying in %.1fs",
                        device, type(exc).__name__, attempt, max_attempts, wait)
        if attempt < max_attempts:
            sleep(wait)
    assert last_exc is not None
    raise last_exc
```

Then make `fetch_with_retry` delegate (keeps its signature/behavior):

```python
def fetch_with_retry(client, day, *, max_attempts=3, base_backoff=2.0, sleep=time.sleep):
    return _call_with_retry(
        lambda: client.fetch_day(day), client.device,
        max_attempts=max_attempts, base_backoff=base_backoff, sleep=sleep,
    )
```

- Add the result type and orchestrator:

```python
_WORKOUT_DEVICES = ("whoop", "google_health")


@dataclass(frozen=True)
class WorkoutResult:
    device: str
    status: str  # "success" | "failed" | "skipped"
    count: int = 0
    error: str | None = None


def pull_workouts(conn, settings, since, until, *, clients=None):
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
            log.warning("%s workouts: failed — %s: %s", entry.device, type(exc).__name__, exc)
            results.append(WorkoutResult(entry.device, "failed", error=str(exc)))
            continue
        with conn:
            for raw in result.raw:
                write_raw_payload(
                    conn, device=entry.device, endpoint=raw.endpoint,
                    date=raw.date, payload=raw.payload,
                )
            for w in result.workouts:
                upsert_workout(conn, w)
        log.info("%s workouts: ok (%d)", entry.device, len(result.workouts))
        results.append(WorkoutResult(entry.device, "success", count=len(result.workouts)))
    return results
```

- [ ] **Step 3b: Wire the CLI flag**

In `src/wearable_pipeline/cli.py`, import `pull_workouts` and add params to `pull`:

```python
from .orchestrator import (
    backfill as orchestrate_backfill,
    exit_code_for,
    pull_one_day,
    pull_workouts,
    summarize,
)
```

Add options to the `pull` signature:

```python
    workouts: bool = typer.Option(
        False, "--workouts", help="Also pull recent workout sessions (Whoop + Google Health)."
    ),
    workout_days: int = typer.Option(
        3, "--workout-days", help="How many days back to pull workouts (default 3)."
    ),
```

After the existing per-device result loop and before `raise typer.Exit(...)`, add:

```python
    if workouts:
        until = day
        since = day - timedelta(days=workout_days - 1)
        wresults = pull_workouts(conn, settings, since, until)
        for r in wresults:
            if r.status == "success":
                typer.echo(f"  {r.device:<14s} OK   {r.count} workouts")
            elif r.status == "failed":
                typer.echo(f"  {r.device:<14s} FAIL {r.error}", err=True)
            else:
                typer.echo(f"  {r.device:<14s} SKIP {r.error}")
        typer.echo(
            f"workouts {since.isoformat()}..{until.isoformat()}: "
            f"{sum(r.count for r in wresults)} total"
        )
```

(`timedelta` is already imported in `cli.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS (new test + all existing retry/orchestrator tests still green).

- [ ] **Step 5: Manual CLI smoke check**

Run: `uv run wearable pull --date today --workouts --workout-days 3`
Expected: per-device metric lines, then workout lines like `whoop OK 2 workouts` / `google_health OK 1 workouts`, then a totals line. (Requires live creds; if Google fails, it reports FAIL without aborting Whoop.)

- [ ] **Step 6: Checkpoint**

Run: `uv run pytest -q`. Confirm green.

---

### Task 7: Viz "Workout comparison" section

**Files:**
- Modify: `src/wearable_pipeline/viz.py`
- Test: manual (Streamlit). No unit test — `match_workouts` is already covered in Task 4.

**Interfaces:**
- Consumes: `workouts.match_workouts`, `models.Workout`, the existing `db_path` and sidebar `start`/`end` date filter.
- Produces: a new section rendered after the Spearman section.

- [ ] **Step 1: Add a loader for workouts**

In `src/wearable_pipeline/viz.py`, add near `_load_daily_metrics`:

```python
@st.cache_data(ttl=60)
def _load_workouts(db_path: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query("SELECT * FROM workouts", conn)
    if not df.empty:
        df["start_time"] = pd.to_datetime(df["start_time"])
        df["date"] = pd.to_datetime(df["date"])
    return df
```

Wrap in `try/except` for the case where the table doesn't exist yet (fresh DB pre-migration): catch `pd.errors.DatabaseError` / `sqlite3.OperationalError` and return `pd.DataFrame()`.

- [ ] **Step 2: Render the comparison section**

In `_render`, after the Spearman section and before the raw-data expander, add:

```python
    # ---- section 4: workout comparison ----------------------------------
    st.subheader("Workout comparison (Whoop vs Google Health)")
    st.caption(
        "Same workout, measured independently by each device, matched by "
        "overlapping time window. Whoop's calories include BMR, so they read "
        "higher than Fitbit's active-only number — that gap is expected, not a bug."
    )
    wdf = _load_workouts(str(db_path))
    if wdf.empty:
        st.info("No workouts pulled yet. Run `uv run wearable pull --workouts`.")
    else:
        wmask = (wdf["date"] >= pd.Timestamp(start)) & (wdf["date"] <= pd.Timestamp(end))
        wview = wdf.loc[wmask]
        whoop_w = [_row_to_workout(r) for _, r in wview[wview["device"] == "whoop"].iterrows()]
        google_w = [_row_to_workout(r) for _, r in wview[wview["device"] == "google_health"].iterrows()]
        from wearable_pipeline.workouts import match_workouts
        res = match_workouts(whoop_w, google_w)
        if res.pairs:
            rows = []
            for p in res.pairs:
                rows.append({
                    "start (local)": p.whoop.start_time.tz_convert(None) if hasattr(p.whoop.start_time, "tz_convert") else p.whoop.start_time,
                    "sport (whoop)": p.whoop.sport,
                    "avg_hr whoop": p.whoop.avg_hr,
                    "avg_hr google": p.google.avg_hr,
                    "avg_hr Δ": _delta(p.whoop.avg_hr, p.google.avg_hr),
                    "max_hr whoop": p.whoop.max_hr,
                    "max_hr google": p.google.max_hr,
                    "max_hr Δ": _delta(p.whoop.max_hr, p.google.max_hr),
                    "cal whoop": p.whoop.calories,
                    "cal google": p.google.calories,
                    "cal Δ": _delta(p.whoop.calories, p.google.calories),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No matched workout pairs in this date range.")
        if res.unmatched_whoop or res.unmatched_google:
            st.caption(
                f"Unmatched — whoop: {len(res.unmatched_whoop)}, "
                f"google_health: {len(res.unmatched_google)} "
                "(a session only one device recorded, or windows that didn't overlap)."
            )
```

- [ ] **Step 3: Add the helpers**

In `src/wearable_pipeline/viz.py`, add module-level helpers (import `Workout` at top: `from wearable_pipeline.models import Workout`):

```python
def _row_to_workout(r: pd.Series) -> Workout:
    return Workout(
        device=r["device"],
        provider_id=r["provider_id"],
        sport=r.get("sport"),
        start_time=r["start_time"].to_pydatetime(),
        end_time=r["end_time"] and pd.to_datetime(r["end_time"]).to_pydatetime() if pd.notna(r.get("end_time")) else None,
        duration_minutes=_int_or_none_series(r.get("duration_minutes")),
        avg_hr=_float_or_none(r.get("avg_hr")),
        max_hr=_float_or_none(r.get("max_hr")),
        calories=_int_or_none_series(r.get("calories")),
        date=r["date"].date(),
    )


def _delta(a: object, b: object) -> object:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return None
    return round(float(a) - float(b), 1)


def _float_or_none(v: object) -> float | None:
    return None if v is None or pd.isna(v) else float(v)


def _int_or_none_series(v: object) -> int | None:
    return None if v is None or pd.isna(v) else int(v)
```

> Keep these robust to pandas `NaN`. If `end_time` handling above is awkward, simplify: `end = pd.to_datetime(r["end_time"]).to_pydatetime() if pd.notna(r.get("end_time")) else None`.

- [ ] **Step 4: Manual smoke check**

Run: `uv run wearable viz` (after a `--workouts` pull). Open `http://localhost:8501`, confirm the "Workout comparison" section shows matched pairs with avg/max HR and calorie columns plus deltas, the calorie caveat caption, and the unmatched count line.

- [ ] **Step 5: Checkpoint**

Run: `uv run pytest -q`. Confirm green (no regressions; viz has no unit tests).

---

## Self-Review notes

- **Spec coverage:** schema (Task 1), models + storage (Task 2), Whoop client (Task 3), matching (Task 4), Google client + live-probe gate (Task 5), pull wiring/`--workouts`/`--workout-days` (Task 6), viz section + calorie caveat + unmatched display (Task 7). All spec sections map to a task.
- **Type consistency:** `fetch_workouts(since, until) -> WorkoutFetchResult` identical on both clients; `match_workouts(whoop, google) -> MatchResult` with `WorkoutPair(whoop, google, overlap_minutes)`; `WorkoutResult(device, status, count, error)` used in Task 6 test and impl.
- **Open risk:** Task 5 Step 1 is a live probe; Steps 2/4 depend on its findings and say so explicitly. The gate (stop + document + report) is the honest path if v4 lacks sessions.
- **Git:** repo is not a git repo — "Commit" steps are reframed as full-suite checkpoints in Global Constraints.
```
