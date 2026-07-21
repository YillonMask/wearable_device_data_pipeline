# Workout pull + cross-device comparison — design

**Date:** 2026-06-26
**Status:** Approved (design); ready for implementation planning

## Goal

Pull recent **workout** sessions from **Whoop** and **Google Health (Fitbit
Air)**, store them locally, and add a section to the Streamlit viz that compares
the *same* workout as measured independently by each device.

This is a new data path. The existing pipeline stores **daily aggregates** (one
`daily_metrics` row per `date`+`device`). Workouts are many-per-day individual
sessions, so they get their own table and their own fetch path — they do **not**
fold into `daily_metrics`.

## Decisions (resolved during brainstorming)

- **Scope:** last **3 days**, **Whoop + Google Health only**. Oura is excluded
  (not requested), even though it exposes workouts.
- **Compare fields:** **average HR, max HR, calories**. Duration and sport are
  also stored — needed for matching and context — but the comparison surfaces HR
  and calories.
- **Trigger:** fold into `wearable pull` via an opt-in `--workouts` flag (with
  `--workout-days N`, default 3). Opt-in so the daily morning routine's behavior
  does not silently change.
- **Architecture:** raw `workouts` table; matching of "same workout" across
  devices is computed at view time (a pure function), not materialized. Keeps the
  raw-first/persistence-first architecture and lets the match algorithm evolve
  without a lossy stored join.

## Open risk

Whoop's workout endpoint (`/developer/v2/activity/workout`) is well-defined.
Whether **Google Health v4 exposes per-workout/session data for Fitbit** (vs.
only daily rollups) to a third-party OAuth client is **unverified**. This must be
probed against the live v4 API during implementation. If Google Health does not
expose individual sessions, the comparison cannot be built as specced — stop and
report back to the owner rather than fabricate or approximate data. (Consistent
with the project's empirical "no Fitbit readiness/sleep-score in v4" finding —
the v4 surface is narrower than the mobile app.)

## Data model

### Migration: `migrations/0004_workouts.sql`

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

- `PRIMARY KEY (device, provider_id)` → re-pulls upsert cleanly (same idempotency
  as `daily_metrics`).
- `date` is the **local-timezone** date of `start_time`, used only for the pull
  window and the viz date filter.
- Dates/timestamps stay ISO-8601 TEXT (Postgres-portable, per the existing
  SQLite-now/Postgres-later rule). No SQLite-specific types.

### Models (`models.py`)

```python
class Workout(BaseModel):
    device: Device                 # restricted to whoop | google_health in practice
    provider_id: str
    sport: str | None = None
    start_time: datetime
    end_time: datetime | None = None
    duration_minutes: int | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    calories: int | None = None
    date: date                     # local-tz date of start_time

@dataclass(frozen=True)
class WorkoutFetchResult:
    workouts: list[Workout]
    raw: list[RawPayload]
```

Fields a device does not expose stay `None` — never substitute zeros (same rule
as `DailyMetrics`).

## Client methods (storage-agnostic)

Each client gains `fetch_workouts(since: date, until: date) -> WorkoutFetchResult`.
Clients build `RawPayload` entries alongside normalized `Workout`s and import no
sqlite — identical contract to `fetch_day`.

### `WhoopClient.fetch_workouts`

- `GET /developer/v2/activity/workout` with `start`/`end` (UTC ISO) covering the
  range, paged via `limit`/`nextToken` if needed.
- Mapping per workout record:
  - `provider_id` ← `id`
  - `sport` ← v2 sport name (`sport_name`, falling back to a `sport_id` lookup if
    only the id is present)
  - `start_time`/`end_time` ← `start`/`end`
  - `duration_minutes` ← `(end - start)` in minutes
  - `avg_hr` ← `score.average_heart_rate`
  - `max_hr` ← `score.max_heart_rate`
  - `calories` ← `score.kilojoule / 4.184` (active-cal; note Whoop's includes BMR
    — flagged in viz)
  - `date` ← local-tz date of `start`

### `GoogleHealthClient.fetch_workouts`

- Fitbit workout/exercise **sessions** via the v4 API, source-filtered to
  `dataSource.platform == "FITBIT"`; **never** `dataPoints:reconcile`.
- Exact data-type id and response shape are **unknown** and must be probed live
  (see Open risk). Map the same target fields (`provider_id`, `sport`,
  start/end, avg/max HR, calories, date). HR may come from a session summary or
  require a companion HR data type — determine during probing.

## Storage (`storage.py`)

```python
def upsert_workout(conn, workout: Workout) -> None:
    # ON CONFLICT(device, provider_id) DO UPDATE on all non-key columns
```

Raw payloads use the existing append-only `write_raw_payload`. Write raw **before**
upserting workouts, in one transaction (raw-first rule).

## Pull wiring

### Orchestrator (`orchestrator.py`)

New `pull_workouts(conn, settings, since, until, *, clients=None)`:
- Iterates only the Whoop + Google Health entries from `enabled_clients`
  (skips Oura).
- Per device: `fetch_with_retry`-style bounded retry on the same transient
  classes; per-device `try/except` for partial-failure tolerance.
- Writes raw payloads, then upserts each `Workout`, in one transaction per device.
- Returns per-device results (device, status, workout count, error) for CLI
  reporting.

### CLI (`cli.py`)

`wearable pull` gains:
- `--workouts` (bool, default `False`) — opt in to also pulling workouts.
- `--workout-days N` (int, default `3`) — window = `[today - N + 1 .. today]`.

When `--workouts` is set, after the existing per-day metrics pull, call
`pull_workouts` over the window and print per-device counts (e.g.
`whoop OK 2 workouts`, `google_health OK 2 workouts`). Exit-code semantics for the
metrics pull are unchanged; workout failures are reported but follow the same
partial-failure spirit.

## Matching + viz

### Matching (`workouts.py`)

```python
def match_workouts(whoop: list[Workout], google: list[Workout]) -> MatchResult
```

- **Rule:** pair a Whoop session with a Google session whose `[start, end]`
  windows **overlap in time**. Greedy, each session matched at most once
  (prefer the largest-overlap candidate to avoid bad many-to-one pairings).
- Returns matched pairs plus the **unmatched** sessions from each device, so
  nothing is silently dropped.
- Pure function (no DB, no network) → unit-testable in isolation.

### Viz (`viz.py`)

New section **"Workout comparison"**:
- Loads `workouts` for the selected date range (reuse the sidebar date filter),
  splits by device, runs `match_workouts`.
- Table of matched pairs: per pair, Whoop vs Google **avg HR**, **max HR**,
  **calories**, plus the delta for each.
- Caption noting Whoop's calorie figure includes BMR and reads higher than
  Fitbit's active-only number — an apples-to-oranges caveat, not a bug.
- Unmatched sessions listed below the matched table.
- Reads the DB directly, no network (existing viz contract).

## Testing

- **Whoop client:** respx fixture for `/developer/v2/activity/workout`;
  assert mapping (HR, calorie conversion, duration, local-tz date).
- **Google client:** respx fixture once the live shape is confirmed;
  assert source-filtering to Fitbit and field mapping.
- **`match_workouts`:** overlap pairs, disjoint sessions stay unmatched,
  many-to-one guard (one Whoop session can't claim two Google sessions),
  empty inputs.
- **`upsert_workout`:** insert then re-insert same `(device, provider_id)`
  updates in place (idempotency).

## Non-goals (YAGNI)

- No `workout_matches` table (matching stays computed).
- No Oura workouts.
- No distance/strain/pace columns beyond what HR + calorie comparison needs
  (sport/duration kept only because matching/context require them).
- Workouts are not always-on in `pull`; opt-in flag only.
