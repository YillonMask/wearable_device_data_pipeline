# Task 4 Report: `match_workouts` pure function

## Files Created

- `src/wearable_pipeline/workouts.py` — pure matching module; exports `WorkoutPair`, `MatchResult`, `match_workouts`.
- `tests/test_workouts_match.py` — 4 unit tests covering all specified scenarios.

## TDD Workflow

**Step 1:** Created `tests/test_workouts_match.py` with 4 tests transcribed exactly from the plan.

**Step 2 — Confirmed FAIL:**
```
$ uv run pytest tests/test_workouts_match.py -v
ERROR: ModuleNotFoundError: No module named 'wearable_pipeline.workouts'
```

**Step 3:** Created `src/wearable_pipeline/workouts.py` with `WorkoutPair`, `MatchResult`, `_end`, `_overlap_minutes`, and `match_workouts` transcribed exactly from the plan.

**Step 4 — Confirmed PASS (all 4):**
```
$ uv run pytest tests/test_workouts_match.py -v
tests/test_workouts_match.py::test_overlapping_sessions_pair PASSED
tests/test_workouts_match.py::test_disjoint_sessions_stay_unmatched PASSED
tests/test_workouts_match.py::test_each_session_matched_at_most_once PASSED
tests/test_workouts_match.py::test_empty_inputs PASSED
4 passed in 0.05s
```

**Step 5 — Full suite checkpoint:**
```
$ uv run pytest -q
9 failed, 78 passed in 6.91s
```
The 9 failures are all pre-existing `test_analysis.py` pandas failures (`ModuleNotFoundError: No module named 'pandas'`). No new failures introduced.

## Concerns

None. Implementation is a verbatim transcription of the plan. The module has no imports beyond stdlib (`dataclasses`, `datetime`) and the local `models.Workout`.
