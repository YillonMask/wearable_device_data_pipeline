# Task 2 Implementation Report: `Workout` model, `WorkoutFetchResult`, and `upsert_workout`

## Files Changed

- `src/wearable_pipeline/models.py` — added `datetime` to the `from datetime import` line; appended `Workout(BaseModel)` class and `WorkoutFetchResult` dataclass.
- `src/wearable_pipeline/storage.py` — added `Workout` to the models import; appended `upsert_workout(conn, workout)` function.
- `tests/test_storage.py` — added `test_upsert_workout_inserts_then_updates` test.

## Test Commands and Output

### Step 2: Confirmed failing (ImportError before model existed)

```
uv run pytest tests/test_storage.py::test_upsert_workout_inserts_then_updates -v
FAILED - ImportError: cannot import name 'Workout' from 'wearable_pipeline.models'
```

### Step 4: Target test passing

```
uv run pytest tests/test_storage.py::test_upsert_workout_inserts_then_updates -v
PASSED  [100%]
1 passed in 0.10s
```

### Step 5: Full suite

```
uv run pytest -q
9 failed, 73 passed in 6.91s
```

## No New Failures

The 9 failures are all pre-existing `tests/test_analysis.py` failures caused by `ModuleNotFoundError: No module named 'pandas'` (the `analysis` extra is not installed in the base dev env). No new failures were introduced.

## Concerns

One minor deviation from the plan's exact test transcription: the plan's `assert row == ("running", 145.0, 178.0, 478)` form doesn't work as-is because `db.connect()` sets `row_factory = sqlite3.Row`, and `sqlite3.Row` objects do not compare equal to tuples in Python 3.11. The assertions were adjusted to use `tuple(row)` and `[tuple(r) for r in rows]` — same semantics, compatible with the connection's row factory.
