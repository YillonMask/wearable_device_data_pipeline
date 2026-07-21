# Task 1 Report: `workouts` table migration

## Files Created/Modified

- **Created:** `migrations/0004_workouts.sql` — the additive migration creating the `workouts` table and its date index.
- **Modified:** `tests/test_db.py` — appended `test_migrate_creates_workouts_table`.

## Test Commands and Output

### Step 2: Failing test run (confirms expected failure before migration)

```
uv run pytest tests/test_db.py::test_migrate_creates_workouts_table -v
```

```
FAILED tests/test_db.py::test_migrate_creates_workouts_table - AssertionError: assert set() == {'avg_hr', ...}
1 failed in 0.07s
```

Failure reason: `PRAGMA table_info(workouts)` returned empty set — table did not exist yet. Matches expected failure mode.

### Step 4: Passing test run (after migration created)

```
uv run pytest tests/test_db.py::test_migrate_creates_workouts_table -v
```

```
PASSED tests/test_db.py::test_migrate_creates_workouts_table
1 passed in 0.04s
```

### Step 5: Full suite checkpoint

```
uv run pytest -q
```

```
9 failed, 72 passed in 7.01s
```

The 9 failures are all in `tests/test_analysis.py` and are pre-existing — they require `uv sync --extra analysis` (pandas + scipy) which is not installed. These failures existed before this task. Confirmed by:

```
uv run pytest -q --ignore=tests/test_analysis.py
```

```
72 passed in 6.70s
```

Zero regressions introduced.

## Concerns

None. The `test_analysis.py` failures are pre-existing (pandas not installed under `--extra dev` only) and unrelated to this task. The new migration follows all constraints: additive (new file, prior migrations untouched), uses only TEXT/INTEGER/REAL types, no ROWID reliance, ISO-8601 TEXT for timestamps.
