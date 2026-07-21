# Task 6 Report: Orchestrator `pull_workouts` + CLI `--workouts` flag

## Status: DONE

## Files Changed

1. `src/wearable_pipeline/orchestrator.py`
   - Extended import of `from .models import ...` to include `Workout, WorkoutFetchResult`
   - Extended import of `from .storage import ...` to include `upsert_workout`
   - Added `_call_with_retry(call, device, *, max_attempts, base_backoff, sleep)` — generic retry helper that encapsulates the existing retry/backoff logic
   - Refactored `fetch_with_retry` to delegate to `_call_with_retry` (behavior-preserving; all existing tests still pass)
   - Added `WorkoutResult` frozen dataclass: `device, status, count=0, error=None`
   - Added `_WORKOUT_DEVICES = ("whoop", "google_health")`
   - Added `pull_workouts(conn, settings, since, until, *, clients=None) -> list[WorkoutResult]` — skips Oura via `continue` before touching `entry.client`; binds loop variable with `lambda e=entry: ...` to avoid late-binding closure bug; writes raw payloads before upserting workouts in a single `with conn:` transaction per device

2. `src/wearable_pipeline/cli.py`
   - Added `pull_workouts` to the orchestrator import block
   - Added `--workouts` (bool, default False) and `--workout-days` (int, default 3) options to the `pull` command
   - Added workouts block after the per-device result loop and before `raise typer.Exit(...)`: calls `pull_workouts`, prints per-device OK/FAIL/SKIP lines, prints totals line

3. `tests/test_orchestrator.py`
   - Added `test_pull_workouts_persists_and_reports`: passes a `FakeWhoop` entry and a bare `object()` Oura entry (which must never be called), asserts Oura is excluded from results, Whoop status is "success" with count=1, one row in `workouts` table, one row in `raw_payloads`

## Test Commands and Output

### New test alone (TDD fail confirmation):
```
uv run pytest tests/test_orchestrator.py::test_pull_workouts_persists_and_reports -v
# FAILED — ImportError: cannot import name 'pull_workouts'
```

### All orchestrator tests after implementation:
```
uv run pytest tests/test_orchestrator.py -v
# 17 passed in 6.15s
```

### Full suite:
```
uv run pytest -q
# 9 failed, 79 passed in 6.94s
# All 9 failures are pre-existing test_analysis.py failures (ModuleNotFoundError: No module named 'pandas')
# Zero new failures introduced
```

### CLI help output showing new flags:
```
uv run wearable pull --help

 Usage: wearable pull [OPTIONS]

 Pull one day across all configured devices (Oura, Whoop, Google Health).

 Exits 0 if all configured devices succeeded, 2 on partial failure, 1 if
 all configured devices failed (or none have credentials).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --date                TEXT     ISO date (YYYY-MM-DD) or 'yesterday'.         │
│                                [default: yesterday]                          │
│ --workouts                     Also pull recent workout sessions (Whoop +    │
│                                Google Health).                               │
│ --workout-days        INTEGER  How many days back to pull workouts (default  │
│                                3).                                           │
│                                [default: 3]                                  │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╝
```

## Deviations

None. Implementation follows the plan exactly. `Workout`, `WorkoutFetchResult`, and `upsert_workout` were already implemented (Tasks 1-2 already done), so no re-implementation was needed — just importing them into the orchestrator.

## Concerns

None. The `_call_with_retry` refactor is behavior-preserving (all 7 existing `fetch_with_retry` tests still pass). The Oura-exclusion guard (`if entry.device not in _WORKOUT_DEVICES: continue`) correctly prevents the bare `object()` Oura client in the test from ever being touched.
