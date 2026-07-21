# Task 3 Report: WhoopClient.fetch_workouts

## Files Changed

- `src/wearable_pipeline/clients/whoop.py` — Added `WORKOUT_ENDPOINT`, updated models import to include `Workout, WorkoutFetchResult`, added `fetch_workouts` and `_normalize_workout` methods.
- `tests/test_whoop_client.py` — Added `WORKOUT_ENDPOINT_PATH` module constant and `test_fetch_workouts_maps_fields` test.

## Test Client Construction (existing pattern mirrored)

No `whoop_client_factory` fixture exists in the codebase. Existing tests use:
- `@respx.mock` decorator (not the `respx_mock` pytest fixture from conftest)
- `_stub_token()` helper to stub a token-endpoint POST
- `_make_client(env_file)` to construct a `WhoopClient` with real `env_file` from `tmp_path`
- `respx.get(BASE_URL + ENDPOINT)` to stub HTTP GET calls

The new test `test_fetch_workouts_maps_fields` follows this exact pattern: `@respx.mock` decorator, `_stub_token()`, `_make_client(env_file)` for client construction, and `respx.get(BASE_URL + WORKOUT_ENDPOINT_PATH)` to stub the workout endpoint.

## Test Commands + Output

**Failing step (before implementation):**
```
uv run pytest tests/test_whoop_client.py::test_fetch_workouts_maps_fields -v
FAILED — AttributeError: 'WhoopClient' object has no attribute 'fetch_workouts'
```

**Passing step (after implementation):**
```
uv run pytest tests/test_whoop_client.py::test_fetch_workouts_maps_fields -v
PASSED — 1 passed in 0.08s
```

**Full suite checkpoint:**
```
uv run pytest -q
9 failed, 74 passed in 6.93s
```
All 9 failures are pre-existing `test_analysis.py` failures (ModuleNotFoundError: No module named 'pandas' — requires `--extra analysis`). Zero new failures.

## Deviations from Plan Code

None. The implementation matches the plan exactly:
- `WORKOUT_ENDPOINT = "v2/activity/workout"` added next to the other endpoint constants
- `Workout, WorkoutFetchResult` added to the models import
- `fetch_workouts` and `_normalize_workout` implemented verbatim from the plan

The test was adapted (as instructed in the plan) to not use the non-existent `whoop_client_factory` fixture — instead it inline-constructs the client using the existing `_make_client(env_file)` pattern.

Note: `Workout` and `WorkoutFetchResult` were already present in `models.py` (Tasks 1 and 2 had already been completed). No model changes were needed.

## Concerns

None. Implementation is clean, tests pass, no regressions introduced.
