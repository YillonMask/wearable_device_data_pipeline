# Self-reported morning readiness + manual Fitbit readiness

**Date:** 2026-06-22
**Status:** Draft, pending implementation

## Goal

Capture two pieces of data that the daily-batch wearable APIs cannot provide:

1. **Subjective readiness.** A user-reported 1–10 score logged each morning *before* looking at any device data, so the rating is not anchored on what the wearables already concluded.
2. **Google Health's readiness score.** Visible in the Fitbit phone app but not exposed by the Google Health v4 API (confirmed empirically on 2026-06-22 and documented in `CLAUDE.md`). The user can read it off the phone and enter it manually, restoring the third device's `readiness_score` series for cross-device Spearman analysis.

Both values feed into `wearable analyze`:

- The subjective score becomes a new virtual "device" named `self`, rank-correlated against each wearable's `readiness_score` (and incidentally any other metric where both sides have data).
- The manual Fitbit readiness fills the gap in `google_health.readiness_score`, restoring the `(oura, google_health)` and `(whoop, google_health)` readiness pairs.

## Non-goals (this iteration)

- **No dashboard surface.** Output is purely a Spearman extension. The Streamlit `wearable viz` page is not modified yet.
- **No notes / tags column** on the subjective log — single number only.
- **No tool-enforced ordering.** `wearable pull` does not refuse to run if the day's log is missing. Anchoring discipline is supported only by `wearable log` never displaying any data from the database.
- **Launchd automation unchanged.** The morning unattended pull keeps running regardless of whether the user has logged.

## Schema

A single new migration: `migrations/0002_self_report_and_manual_metrics.sql`. Two tables — kept separate so each one's contract is clear.

```sql
-- Subjective readiness log: one 1–10 score per morning, user-entered.
CREATE TABLE IF NOT EXISTS self_report (
    date       TEXT PRIMARY KEY,                              -- ISO date (YYYY-MM-DD)
    readiness  INTEGER NOT NULL CHECK (readiness BETWEEN 1 AND 10),
    logged_at  TEXT NOT NULL                                  -- ISO timestamp (UTC)
);

-- Manually-entered readiness for Google Health (Fitbit Air): visible in the
-- Fitbit phone app but never returned by the v4 API. Kept separate from
-- daily_metrics so daily_metrics remains strictly "API-derived data only" and
-- the morning pull's UPSERT cannot clobber a manually-entered value with NULL.
CREATE TABLE IF NOT EXISTS manual_metrics (
    date             TEXT NOT NULL,
    device           TEXT NOT NULL,                          -- 'google_health'
    readiness_score  INTEGER NOT NULL CHECK (readiness_score BETWEEN 0 AND 100),
    logged_at        TEXT NOT NULL,
    PRIMARY KEY (date, device)
);
```

### Why two tables (not one row in `daily_metrics`)

- `daily_metrics` is documented in `CLAUDE.md` as one row per `(date, device)` of **API-derived** data, with the `device` enum strictly `'oura' | 'whoop' | 'google_health'`. A `device='self'` row would widen that contract; a manually-entered Fitbit readiness landing in `daily_metrics` would conflict with the pull's UPSERT semantics (the morning pull would write `readiness_score = NULL` from the API, overwriting the manual entry).
- The subjective score uses a 1–10 scale; device scores are 0–100. Storing both in the same column reads cleanly only if a future reader knows the scale-by-device convention. Two tables makes the scale obvious from the schema.
- `manual_metrics` generalizes — any future "I saw this on the device app but the API doesn't return it" value has a clear home.

## CLI: `wearable log`

Single new Typer command. Captures both pieces in one ritual.

### Interactive (default)

```
$ uv run wearable log
Your readiness 1–10: 7
Fitbit readiness 0–100 (Enter to skip): 48
Logged: self=7, google_health.readiness_score=48 for 2026-06-22
```

### One-shot forms

```bash
uv run wearable log 7                                    # subjective only
uv run wearable log --fitbit-readiness 48                # Fitbit manual only
uv run wearable log 7 --fitbit-readiness 48              # both, no prompts
uv run wearable log --date 2026-06-21 5                  # backfill subjective
uv run wearable log --date 2026-06-21 5 --fitbit-readiness 50  # backfill both
```

### Behavior

- **Date** defaults to "today" in `LOCAL_TIMEZONE` (the constant already used by the Whoop client). `--date YYYY-MM-DD` overrides for backfill.
- **Re-logging** the same date overwrites: `ON CONFLICT(date) DO UPDATE SET readiness = excluded.readiness, logged_at = excluded.logged_at` (and analogous for `manual_metrics.readiness_score`).
- **Validation:** invalid input (out of range, non-integer, missing required value) exits non-zero with a clear message and writes nothing to the database.
- **Anchoring protection:** the command **never prints any data from `daily_metrics`, `self_report`, or `manual_metrics` for that day before or after writing.** Output is limited to confirming what was just logged. This is the only mechanism that protects against subjective-rating anchoring.
- **Prompt rules** in interactive mode:
  - If neither positional nor `--fitbit-readiness` is given: prompt for both, in that order.
  - If only the positional is given: prompt for Fitbit (Enter to skip).
  - If only `--fitbit-readiness` is given: prompt for subjective (Enter to skip).
  - If both given: no prompts.
- **Skip semantics:** an empty (Enter) prompt response writes nothing to that table — it is a true skip, not a NULL write. If both prompts are skipped in interactive mode the command prints `Nothing logged` and exits non-zero, so the user knows nothing was recorded.

## Storage (`storage.py`)

Two new functions, mirroring the existing style of `upsert_daily_metrics`:

```python
def upsert_self_report(conn: sqlite3.Connection, *, day: date, readiness: int) -> None: ...

def upsert_manual_readiness(
    conn: sqlite3.Connection,
    *,
    day: date,
    device: str,
    readiness_score: int,
) -> None: ...
```

Both commit in one transaction. `logged_at` uses `datetime.now(timezone.utc).isoformat()` like the existing helpers.

## Analysis (`analysis.py`)

Extend `run_spearman` to incorporate both new tables. Two changes:

1. **COALESCE manual readiness into `google_health.readiness_score`** at read time. The base query becomes:

   ```sql
   SELECT
     dm.date,
     dm.device,
     COALESCE(mm.readiness_score, dm.readiness_score) AS readiness_score,
     dm.sleep_score,
     dm.strain_or_activity_score,
     dm.hrv_ms,
     dm.resting_hr,
     dm.total_sleep_minutes
   FROM daily_metrics dm
   LEFT JOIN manual_metrics mm
     ON mm.date = dm.date AND mm.device = dm.device
   {where_clause}
   ```

   `(oura, google_health)` and `(whoop, google_health)` on `readiness_score` start producing real correlations once enough manual entries accumulate.

2. **Add `self` as a virtual device** in the wide DataFrame. After the pivot, splice in a `self` column built from `self_report.readiness` joined on `date`. Other metric columns are NaN for `self`, which the existing `dropna()` handles naturally — `self`-pair correlations only run for `readiness_score`, and for other metrics the pair returns `n=0, rho=None` exactly like a device that lacks the field.

3. **New default pairs:**

   ```python
   DEFAULT_PAIRS = (
       ("oura", "whoop"),
       ("oura", "google_health"),
       ("whoop", "google_health"),
       ("self", "oura"),
       ("self", "whoop"),
       ("self", "google_health"),
   )
   ```

   The existing `min_n=14` threshold applies to all pairs uniformly.

The principle from `CLAUDE.md` — "never impute missing values; drop rows per pair" — is preserved. The 1–10 vs. 0–100 scale mismatch is a non-issue: Spearman ranks each series independently.

## Tests

Following the existing patterns in `tests/`:

- **`test_storage.py`**
  - `upsert_self_report` writes a row; re-log for the same date updates `readiness` and bumps `logged_at`.
  - `upsert_manual_readiness` writes a row; re-call updates `readiness_score` and bumps `logged_at`; CHECK constraint rejects out-of-range values.
- **`test_cli.py`**
  - `log 7` writes one row to `self_report`, none to `manual_metrics`.
  - `log 7 --fitbit-readiness 48` writes to both.
  - `log --fitbit-readiness 48` writes to `manual_metrics` only.
  - `log 11`, `log -1`, `log abc`, `log --fitbit-readiness 101` exit non-zero, write nothing.
  - `log --date 2026-06-20 5` writes to the specified past date.
  - Interactive mode: stub stdin, verify prompts and writes.
- **`test_analysis.py`**
  - Fixture with ≥14 days of `self_report` and `manual_metrics` + matching `daily_metrics`: verify the three new `self` pairs return real rho on `readiness_score`, return `n=0, rho=None` on other metrics; verify `(oura, google_health)` and `(whoop, google_health)` `readiness_score` use the COALESCEd manual values.
- **Migration**: a smoke test (or reusing the existing init test) confirms `0002_self_report.sql` applies cleanly and is idempotent.

## Files touched

- `migrations/0002_self_report.sql` (new)
- `src/wearable_pipeline/storage.py` (+ `upsert_self_report`, `upsert_manual_readiness`)
- `src/wearable_pipeline/cli.py` (+ `log` command)
- `src/wearable_pipeline/analysis.py` (extend `run_spearman` query and `DEFAULT_PAIRS`)
- `tests/test_storage.py`, `tests/test_cli.py`, `tests/test_analysis.py` (new test cases)
- `CLAUDE.md` (small addition under "Daily morning convention" telling future Claude to remind the user to run `wearable log` *before* pulling)

## Migration considerations

- Purely additive — no changes to existing tables, no data backfill.
- Manual readiness for days already in `daily_metrics` is supported via `wearable log --date YYYY-MM-DD --fitbit-readiness N`.

## Risks / open questions

- **Anchoring discipline depends on user habit.** If the user runs `wearable pull` or opens the dashboard before `wearable log`, the subjective value for that day is contaminated. The tool can't fully prevent this; mitigation is keeping `wearable log` silent about device data, but real enforcement is the user's discipline.
- **Manual entries are only useful if logged consistently.** Spearman drops rows where either side is missing, so sporadic manual entries shrink effective N for `google_health` readiness pairs.
