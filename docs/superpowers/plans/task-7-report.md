# Task 7 Report — Viz "Workout comparison" section

## Files changed

- `src/wearable_pipeline/viz.py` — added `from wearable_pipeline.models import Workout` import, `_load_workouts` cached loader, four helper functions (`_float_or_none`, `_int_or_none_series`, `_delta`, `_row_to_workout`), and section 4 render block inserted after the Spearman section and before the raw-data expander.

## Import check

Required `uv sync --extra viz` first (streamlit not present). After that, `uv run python -c "import wearable_pipeline.viz"` succeeded — only Streamlit runtime warnings (expected outside `streamlit run`), no errors or import failures.

## Pytest result

`uv run pytest -q` after `uv sync --extra dev` (which removes pandas/scipy): **9 failed, 80 passed** — the same 9 pre-existing `test_analysis.py` failures (pandas not in the dev-only lockset); no new failures introduced.

## Concerns

None. The `_load_workouts` try/except gracefully handles a fresh DB that hasn't had the `workouts` migration applied yet (returns empty DataFrame, which causes the "No workouts pulled yet" `st.info` message to render). The `match_workouts` import is deferred inside the `else` branch to avoid a hard import-time dependency on `workouts.py` (which is already present from Task 4).
