# Self-Report Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `wearable log` CLI command that captures (1) a daily 1–10 subjective readiness score and (2) a manually-reported Google Health (Fitbit Air) readiness 0–100, and extend `wearable analyze` so the subjective score becomes a virtual `self` device and the manual Fitbit value backfills `google_health.readiness_score`.

**Architecture:** Two new tables (`self_report`, `manual_metrics`) introduced via a single SQL migration, kept separate from `daily_metrics` so the existing pull's UPSERT cannot clobber user-entered values. The new `wearable log` Typer command is the only entry point — never displays existing data, preserving the anchoring discipline. `analysis.run_spearman` joins both tables in via SQL (`LEFT JOIN ... COALESCE`) and splices `self` into the wide DataFrame as a synthetic device.

**Tech Stack:** Python 3, SQLite (via `sqlite3` stdlib), Typer, Pydantic v2 (existing), pandas + scipy (existing `analysis` extra). Tests use pytest + `migrated_db` fixture from `tests/conftest.py`.

**Reference spec:** `docs/superpowers/specs/2026-06-22-self-report-readiness-design.md`

**Note on commits:** the working directory is **not** a git repo (verified). Skip the `git commit` step at each checkpoint — just confirm tests pass and move on. If the project is later initialized as a git repo, fold the work into commits at task boundaries.

---

## Task 1: Migration `0002_self_report_and_manual_metrics.sql`

**Files:**
- Create: `migrations/0002_self_report_and_manual_metrics.sql`
- Test (extend): `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Open `tests/test_db.py` and add the following test function at the bottom (after the existing tests):

```python
def test_migration_0002_creates_self_report_and_manual_metrics(tmp_path) -> None:
    from wearable_pipeline import db

    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "self_report" in tables
    assert "manual_metrics" in tables

    self_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(self_report)")
    }
    assert self_cols == {"date", "readiness", "logged_at"}

    manual_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(manual_metrics)")
    }
    assert manual_cols == {"date", "device", "readiness_score", "logged_at"}

    # CHECK constraints reject out-of-range values.
    import sqlite3 as _sq

    import pytest as _pt

    with _pt.raises(_sq.IntegrityError):
        conn.execute(
            "INSERT INTO self_report (date, readiness, logged_at) "
            "VALUES ('2026-06-22', 11, '2026-06-22T08:00:00+00:00')"
        )
    with _pt.raises(_sq.IntegrityError):
        conn.execute(
            "INSERT INTO manual_metrics (date, device, readiness_score, logged_at) "
            "VALUES ('2026-06-22', 'google_health', 101, '2026-06-22T08:00:00+00:00')"
        )

    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/xinruiyi/Documents/wearable_devices_datapipeline
uv run pytest tests/test_db.py::test_migration_0002_creates_self_report_and_manual_metrics -v
```

Expected: FAIL with `AssertionError: assert 'self_report' in tables` (the tables don't exist yet).

- [ ] **Step 3: Create the migration file**

Write `migrations/0002_self_report_and_manual_metrics.sql` with this exact content:

```sql
-- Subjective readiness log: one 1–10 score per morning, user-entered before
-- any wearable data is viewed, to keep the rating un-anchored.
CREATE TABLE IF NOT EXISTS self_report (
    date       TEXT PRIMARY KEY,
    readiness  INTEGER NOT NULL CHECK (readiness BETWEEN 1 AND 10),
    logged_at  TEXT NOT NULL
);

-- Manually-entered readiness for Google Health (Fitbit Air): visible in the
-- Fitbit phone app but never returned by the v4 API (confirmed empirically
-- 2026-06-22 — see CLAUDE.md). Kept separate from daily_metrics so
-- daily_metrics remains strictly API-derived and the morning pull's UPSERT
-- cannot clobber a manually-entered value with NULL.
CREATE TABLE IF NOT EXISTS manual_metrics (
    date             TEXT NOT NULL,
    device           TEXT NOT NULL,
    readiness_score  INTEGER NOT NULL CHECK (readiness_score BETWEEN 0 AND 100),
    logged_at        TEXT NOT NULL,
    PRIMARY KEY (date, device)
);
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_db.py::test_migration_0002_creates_self_report_and_manual_metrics -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite to confirm nothing else broke**

```bash
uv run pytest -q
```

Expected: all tests pass (~57 tests, one more than before).

---

## Task 2: `upsert_self_report` storage helper

**Files:**
- Modify: `src/wearable_pipeline/storage.py`
- Test (extend): `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py`:

```python
import pytest


def test_upsert_self_report_round_trip(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_self_report

    upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=7)

    row = migrated_db.execute(
        "SELECT date, readiness, logged_at FROM self_report"
    ).fetchone()
    assert row["date"] == "2026-06-22"
    assert row["readiness"] == 7
    assert row["logged_at"]  # non-empty ISO timestamp


def test_upsert_self_report_overwrites_existing(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_self_report

    upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=7)
    first_logged_at = migrated_db.execute(
        "SELECT logged_at FROM self_report"
    ).fetchone()["logged_at"]

    upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=5)

    rows = migrated_db.execute(
        "SELECT readiness, logged_at FROM self_report"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["readiness"] == 5
    assert rows[0]["logged_at"] >= first_logged_at  # bumped (or equal at worst)


def test_upsert_self_report_rejects_out_of_range(migrated_db) -> None:
    import sqlite3

    from wearable_pipeline.storage import upsert_self_report

    with pytest.raises(sqlite3.IntegrityError):
        upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=11)
    with pytest.raises(sqlite3.IntegrityError):
        upsert_self_report(migrated_db, day=date(2026, 6, 22), readiness=0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_storage.py -v -k self_report
```

Expected: FAIL with `ImportError: cannot import name 'upsert_self_report'`.

- [ ] **Step 3: Add the function to `storage.py`**

Append to `src/wearable_pipeline/storage.py` (after the existing `upsert_daily_metrics`):

```python
def upsert_self_report(
    conn: sqlite3.Connection, *, day, readiness: int
) -> None:
    conn.execute(
        "INSERT INTO self_report (date, readiness, logged_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET "
        "  readiness = excluded.readiness, "
        "  logged_at = excluded.logged_at",
        (
            day.isoformat(),
            readiness,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
```

`day` is typed implicitly as `datetime.date` (the existing module already uses `datetime` from stdlib; no new import needed beyond what's there).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_storage.py -v -k self_report
```

Expected: 3 PASS.

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all pass.

---

## Task 3: `upsert_manual_readiness` storage helper

**Files:**
- Modify: `src/wearable_pipeline/storage.py`
- Test (extend): `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py`:

```python
def test_upsert_manual_readiness_round_trip(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_manual_readiness

    upsert_manual_readiness(
        migrated_db,
        day=date(2026, 6, 22),
        device="google_health",
        readiness_score=48,
    )

    row = migrated_db.execute(
        "SELECT date, device, readiness_score, logged_at FROM manual_metrics"
    ).fetchone()
    assert row["date"] == "2026-06-22"
    assert row["device"] == "google_health"
    assert row["readiness_score"] == 48
    assert row["logged_at"]


def test_upsert_manual_readiness_overwrites_existing(migrated_db) -> None:
    from wearable_pipeline.storage import upsert_manual_readiness

    upsert_manual_readiness(
        migrated_db,
        day=date(2026, 6, 22),
        device="google_health",
        readiness_score=48,
    )
    upsert_manual_readiness(
        migrated_db,
        day=date(2026, 6, 22),
        device="google_health",
        readiness_score=62,
    )

    rows = migrated_db.execute(
        "SELECT readiness_score FROM manual_metrics WHERE device = 'google_health'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["readiness_score"] == 62


def test_upsert_manual_readiness_rejects_out_of_range(migrated_db) -> None:
    import sqlite3

    from wearable_pipeline.storage import upsert_manual_readiness

    with pytest.raises(sqlite3.IntegrityError):
        upsert_manual_readiness(
            migrated_db,
            day=date(2026, 6, 22),
            device="google_health",
            readiness_score=101,
        )
    with pytest.raises(sqlite3.IntegrityError):
        upsert_manual_readiness(
            migrated_db,
            day=date(2026, 6, 22),
            device="google_health",
            readiness_score=-1,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_storage.py -v -k manual_readiness
```

Expected: FAIL with `ImportError: cannot import name 'upsert_manual_readiness'`.

- [ ] **Step 3: Add the function to `storage.py`**

Append to `src/wearable_pipeline/storage.py`:

```python
def upsert_manual_readiness(
    conn: sqlite3.Connection,
    *,
    day,
    device: str,
    readiness_score: int,
) -> None:
    conn.execute(
        "INSERT INTO manual_metrics (date, device, readiness_score, logged_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(date, device) DO UPDATE SET "
        "  readiness_score = excluded.readiness_score, "
        "  logged_at = excluded.logged_at",
        (
            day.isoformat(),
            device,
            readiness_score,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_storage.py -v -k manual_readiness
```

Expected: 3 PASS.

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all pass.

---

## Task 4: `wearable log` CLI command

**Files:**
- Modify: `src/wearable_pipeline/cli.py`
- Create: `tests/test_cli_log.py`

The command supports two paths:
- **Non-interactive:** any combination of the positional `score` arg and the `--fitbit-readiness` option writes those values immediately, no prompts.
- **Interactive:** if `--no-prompt` is NOT set AND at least one of `(score, --fitbit-readiness)` is missing, the command prompts (via `typer.prompt`) for the missing piece(s). Empty/blank response = skip that field.

The full prompt rules from the spec:
- Neither given: prompt for both, in order.
- Only positional given: prompt for Fitbit (Enter to skip).
- Only `--fitbit-readiness` given: prompt for subjective (Enter to skip).
- Both given: no prompts.
- Both skipped in interactive mode → exit non-zero with `Nothing logged`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_log.py` with this complete content:

```python
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wearable_pipeline import db
from wearable_pipeline.cli import app

runner = CliRunner()


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch) -> Path:
    """Point the CLI at a fresh, migrated DB in tmp_path and return its path."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    # Pre-migrate so we don't depend on the CLI doing it.
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.close()
    return db_path


def _rows(db_path: Path, sql: str):
    conn = db.connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def test_log_positional_writes_self_report(db_env: Path) -> None:
    result = runner.invoke(
        app, ["log", "7", "--date", "2026-06-22", "--no-prompt"]
    )
    assert result.exit_code == 0, result.output
    self_rows = _rows(db_env, "SELECT * FROM self_report")
    manual_rows = _rows(db_env, "SELECT * FROM manual_metrics")
    assert len(self_rows) == 1
    assert self_rows[0]["date"] == "2026-06-22"
    assert self_rows[0]["readiness"] == 7
    assert manual_rows == []


def test_log_fitbit_only_writes_manual_metrics(db_env: Path) -> None:
    result = runner.invoke(
        app,
        ["log", "--fitbit-readiness", "48", "--date", "2026-06-22", "--no-prompt"],
    )
    assert result.exit_code == 0, result.output
    self_rows = _rows(db_env, "SELECT * FROM self_report")
    manual_rows = _rows(db_env, "SELECT * FROM manual_metrics")
    assert self_rows == []
    assert len(manual_rows) == 1
    assert manual_rows[0]["device"] == "google_health"
    assert manual_rows[0]["readiness_score"] == 48


def test_log_both_writes_both_tables(db_env: Path) -> None:
    result = runner.invoke(
        app,
        [
            "log", "7",
            "--fitbit-readiness", "48",
            "--date", "2026-06-22",
            "--no-prompt",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _rows(db_env, "SELECT readiness FROM self_report")[0]["readiness"] == 7
    assert (
        _rows(db_env, "SELECT readiness_score FROM manual_metrics")[0][
            "readiness_score"
        ]
        == 48
    )


def test_log_rejects_subjective_out_of_range(db_env: Path) -> None:
    result = runner.invoke(app, ["log", "11", "--no-prompt"])
    assert result.exit_code != 0
    assert _rows(db_env, "SELECT * FROM self_report") == []


def test_log_rejects_fitbit_out_of_range(db_env: Path) -> None:
    result = runner.invoke(
        app, ["log", "--fitbit-readiness", "101", "--no-prompt"]
    )
    assert result.exit_code != 0
    assert _rows(db_env, "SELECT * FROM manual_metrics") == []


def test_log_nothing_given_with_no_prompt_exits_nonzero(db_env: Path) -> None:
    result = runner.invoke(app, ["log", "--no-prompt"])
    assert result.exit_code != 0
    assert "Nothing logged" in result.output
    assert _rows(db_env, "SELECT * FROM self_report") == []
    assert _rows(db_env, "SELECT * FROM manual_metrics") == []


def test_log_interactive_prompts_for_both(db_env: Path) -> None:
    # stdin: subjective then Fitbit
    result = runner.invoke(
        app, ["log", "--date", "2026-06-22"], input="7\n48\n"
    )
    assert result.exit_code == 0, result.output
    assert _rows(db_env, "SELECT readiness FROM self_report")[0]["readiness"] == 7
    assert (
        _rows(db_env, "SELECT readiness_score FROM manual_metrics")[0][
            "readiness_score"
        ]
        == 48
    )


def test_log_interactive_skip_fitbit(db_env: Path) -> None:
    # Subjective = 7, Fitbit = (Enter to skip)
    result = runner.invoke(
        app, ["log", "--date", "2026-06-22"], input="7\n\n"
    )
    assert result.exit_code == 0, result.output
    assert _rows(db_env, "SELECT readiness FROM self_report")[0]["readiness"] == 7
    assert _rows(db_env, "SELECT * FROM manual_metrics") == []


def test_log_interactive_skip_both_exits_nonzero(db_env: Path) -> None:
    result = runner.invoke(
        app, ["log", "--date", "2026-06-22"], input="\n\n"
    )
    assert result.exit_code != 0
    assert "Nothing logged" in result.output


def test_log_does_not_print_existing_data(db_env: Path) -> None:
    """Anchoring protection: the command must not echo any prior log values."""
    # Pre-populate yesterday's self_report.
    from wearable_pipeline.storage import upsert_self_report

    conn = db.connect(db_env)
    upsert_self_report(conn, day=date(2026, 6, 21), readiness=3)
    conn.close()

    # Now log today and verify yesterday's date doesn't appear in the output
    # (it would only be there if the CLI queried and echoed prior log values).
    result = runner.invoke(
        app, ["log", "7", "--date", "2026-06-22", "--no-prompt"]
    )
    assert result.exit_code == 0
    assert "2026-06-21" not in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_cli_log.py -v
```

Expected: ALL FAIL — `log` command doesn't exist yet, every invocation errors with "No such command 'log'".

- [ ] **Step 3: Add the `log` command to `cli.py`**

Insert the following block in `src/wearable_pipeline/cli.py` between the `pull` command and the `viz` command (i.e., after the `pull` function ends at line ~124 and before `@app.command()\ndef viz(...)`).

First, add this import near the top of the file (with the other `from .` imports, after the existing `from .orchestrator import ...` block — around line 18):

```python
from .storage import upsert_manual_readiness, upsert_self_report
```

Then insert the new command function:

```python
@app.command("log")
def log_cmd(
    score: int | None = typer.Argument(
        None, help="Your subjective readiness, 1–10."
    ),
    fitbit_readiness: int | None = typer.Option(
        None,
        "--fitbit-readiness",
        help="Manually-entered Google Health (Fitbit) readiness, 0–100.",
    ),
    target_date: str = typer.Option(
        "today", "--date", help="ISO date (YYYY-MM-DD), 'today', or 'yesterday'."
    ),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help="Disable interactive prompts (used by tests/scripting).",
    ),
) -> None:
    """Log subjective readiness (1–10) and/or Fitbit readiness (0–100) for a day.

    Never prints existing data — preserves the anchoring discipline of rating
    before viewing wearable numbers.
    """
    day = _parse_date(target_date)
    settings = load_settings()
    conn = db.connect(settings.database_path)
    db.migrate(conn)

    # Interactive prompt fill-in.
    if not no_prompt:
        if score is None:
            raw = typer.prompt(
                "Your readiness 1–10", default="", show_default=False
            ).strip()
            if raw:
                try:
                    score = int(raw)
                except ValueError:
                    typer.echo(f"Invalid subjective score: {raw!r}", err=True)
                    raise typer.Exit(code=2)
        if fitbit_readiness is None:
            raw = typer.prompt(
                "Fitbit readiness 0–100 (Enter to skip)",
                default="",
                show_default=False,
            ).strip()
            if raw:
                try:
                    fitbit_readiness = int(raw)
                except ValueError:
                    typer.echo(f"Invalid Fitbit readiness: {raw!r}", err=True)
                    raise typer.Exit(code=2)

    # Range validation (the DB CHECK would also reject these, but we want a
    # cleaner error and a guaranteed no-write on bad input).
    if score is not None and not (1 <= score <= 10):
        typer.echo(
            f"Subjective readiness must be between 1 and 10 (got {score}).",
            err=True,
        )
        raise typer.Exit(code=2)
    if fitbit_readiness is not None and not (0 <= fitbit_readiness <= 100):
        typer.echo(
            f"Fitbit readiness must be between 0 and 100 (got {fitbit_readiness}).",
            err=True,
        )
        raise typer.Exit(code=2)

    if score is None and fitbit_readiness is None:
        typer.echo("Nothing logged.", err=True)
        raise typer.Exit(code=2)

    parts: list[str] = []
    if score is not None:
        upsert_self_report(conn, day=day, readiness=score)
        parts.append(f"self={score}")
    if fitbit_readiness is not None:
        upsert_manual_readiness(
            conn,
            day=day,
            device="google_health",
            readiness_score=fitbit_readiness,
        )
        parts.append(f"google_health.readiness_score={fitbit_readiness}")

    typer.echo(f"Logged: {', '.join(parts)} for {day.isoformat()}")
```

Important details:
- The function is named `log_cmd` (not `log`) to avoid shadowing the stdlib `logging.log` should anyone import this module.
- `@app.command("log")` registers it as the CLI subcommand `log` regardless of the function name.
- `target_date` defaults to `"today"` (the existing `_parse_date` already handles `"today"`, `"yesterday"`, and ISO dates).
- The command **deliberately reads no rows from the database** beyond what `db.migrate` does — no SELECTs from `self_report`, `manual_metrics`, or `daily_metrics`. This is what enforces the anchoring rule.

- [ ] **Step 4: Run the CLI tests to verify they pass**

```bash
uv run pytest tests/test_cli_log.py -v
```

Expected: 10 PASS.

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all pass.

---

## Task 5: Extend `analysis.run_spearman` to include `self` and COALESCE manual readiness

**Files:**
- Modify: `src/wearable_pipeline/analysis.py`
- Test (extend): `tests/test_analysis.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_analysis.py`:

```python
def test_self_pair_correlates_with_oura_readiness(conn) -> None:
    from wearable_pipeline.analysis import run_spearman
    from wearable_pipeline.storage import upsert_self_report

    start = date(2026, 1, 1)
    oura_vals = [60, 65, 70, 72, 75, 80, 81, 82, 85, 88, 90, 92, 94, 95, 99]
    # Self ranks identically to Oura → Spearman rho = 1.0.
    self_vals = [3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10]
    for i, (o, s) in enumerate(zip(oura_vals, self_vals)):
        d = start + timedelta(days=i)
        upsert_daily_metrics(
            conn, DailyMetrics(date=d, device="oura", readiness_score=o)
        )
        upsert_self_report(conn, day=d, readiness=s)

    rows = run_spearman(conn, min_n=14)
    by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
    rec = by_pair[("readiness_score", "self", "oura")]
    assert rec.n == 15
    assert rec.rho == pytest.approx(1.0)


def test_self_pair_returns_n_zero_for_non_readiness_metrics(conn) -> None:
    """self has no value for sleep_score, hrv_ms, etc. — pair should be n=0."""
    from wearable_pipeline.analysis import run_spearman
    from wearable_pipeline.storage import upsert_self_report

    start = date(2026, 1, 1)
    for i in range(15):
        d = start + timedelta(days=i)
        upsert_daily_metrics(
            conn,
            DailyMetrics(
                date=d, device="oura", sleep_score=80, hrv_ms=45.0
            ),
        )
        upsert_self_report(conn, day=d, readiness=7)

    rows = run_spearman(conn, min_n=14)
    by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
    rec = by_pair[("sleep_score", "self", "oura")]
    assert rec.n == 0
    assert rec.rho is None


def test_manual_readiness_fills_google_health_gap(conn) -> None:
    """Manual google_health readiness is used in (oura, google_health) Spearman."""
    from wearable_pipeline.analysis import run_spearman
    from wearable_pipeline.storage import upsert_manual_readiness

    start = date(2026, 1, 1)
    oura_vals = [60, 65, 70, 72, 75, 80, 81, 82, 85, 88, 90, 92, 94, 95, 99]
    # google_health rows exist but readiness_score is None from the API.
    # Manual entries provide it, ranked identically to oura → rho = 1.0.
    fitbit_manual_vals = [40, 45, 50, 55, 60, 65, 70, 72, 75, 78, 82, 85, 87, 88, 91]
    for i, (o, f) in enumerate(zip(oura_vals, fitbit_manual_vals)):
        d = start + timedelta(days=i)
        upsert_daily_metrics(
            conn, DailyMetrics(date=d, device="oura", readiness_score=o)
        )
        upsert_daily_metrics(
            conn,
            DailyMetrics(date=d, device="google_health", readiness_score=None),
        )
        upsert_manual_readiness(
            conn, day=d, device="google_health", readiness_score=f
        )

    rows = run_spearman(conn, min_n=14)
    by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
    rec = by_pair[("readiness_score", "oura", "google_health")]
    assert rec.n == 15
    assert rec.rho == pytest.approx(1.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_analysis.py -v -k "self_pair or manual_readiness_fills"
```

Expected: FAIL — the `(self, oura)` pair won't be in `by_pair` (KeyError) and the manual-COALESCE one will return rho=None because `google_health.readiness_score` is NULL.

- [ ] **Step 3: Modify `analysis.py`**

Replace the entire body of `src/wearable_pipeline/analysis.py` with the version below. (The whole file is short, so a full replace is cleaner than three separate edits.)

```python
"""Cross-device Spearman rank correlation.

Each device's `*_score` fields use a proprietary scale, so comparing absolute
values is meaningless. Rank correlation is the right signal: do two devices
agree about which days were good and which were bad?

We deliberately do NOT compute mean rho across pairs (averaging devices would
itself be ill-defined) and we never impute missing values — dropping null
rows per-pair preserves the comparison's integrity.

``pandas`` and ``scipy`` are imported lazily inside ``run_spearman`` so the
base CLI stays cheap for users who only ingest data.

The synthetic device ``self`` represents the user's subjective 1–10 readiness
from the ``self_report`` table. It only has a value for ``readiness_score``;
other-metric pairs involving ``self`` return n=0.

Manually-entered values from the ``manual_metrics`` table (currently only
``google_health.readiness_score``, since the Fitbit phone app shows it but the
Google Health v4 API does not return it) override the corresponding NULL in
``daily_metrics`` via ``COALESCE``.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date

DEFAULT_METRICS: tuple[str, ...] = (
    "readiness_score",
    "sleep_score",
    "strain_or_activity_score",
    "hrv_ms",
    "resting_hr",
    "total_sleep_minutes",
)

DEFAULT_PAIRS: tuple[tuple[str, str], ...] = (
    ("oura", "whoop"),
    ("oura", "google_health"),
    ("whoop", "google_health"),
    ("self", "oura"),
    ("self", "whoop"),
    ("self", "google_health"),
)


@dataclass(frozen=True)
class SpearmanRow:
    metric: str
    device_a: str
    device_b: str
    n: int
    rho: float | None
    p: float | None


def run_spearman(
    conn: sqlite3.Connection,
    *,
    since: date | None = None,
    until: date | None = None,
    metrics: tuple[str, ...] = DEFAULT_METRICS,
    pairs: tuple[tuple[str, str], ...] = DEFAULT_PAIRS,
    min_n: int = 14,
) -> list[SpearmanRow]:
    # Lazy imports keep the base CLI free of pandas/scipy.
    import pandas as pd
    from scipy import stats

    where: list[str] = []
    params: list[str] = []
    if since:
        where.append("dm.date >= ?")
        params.append(since.isoformat())
    if until:
        where.append("dm.date <= ?")
        params.append(until.isoformat())
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    # COALESCE manual_metrics.readiness_score over daily_metrics.readiness_score
    # for the google_health rows. Other metrics pass through unchanged.
    metric_select = ", ".join(
        (
            "COALESCE(mm.readiness_score, dm.readiness_score) AS readiness_score"
            if m == "readiness_score"
            else f"dm.{m}"
        )
        for m in metrics
    )
    df = pd.read_sql_query(
        f"SELECT dm.date, dm.device, {metric_select} "
        f"FROM daily_metrics dm "
        f"LEFT JOIN manual_metrics mm "
        f"  ON mm.date = dm.date AND mm.device = dm.device"
        f"{where_clause}",
        conn,
        params=params,
    )

    # Pull self_report and add a synthetic 'self' device row per date, with
    # readiness in the readiness_score column and NaN for everything else.
    self_where = ""
    self_params: list[str] = []
    if since or until:
        clauses: list[str] = []
        if since:
            clauses.append("date >= ?")
            self_params.append(since.isoformat())
        if until:
            clauses.append("date <= ?")
            self_params.append(until.isoformat())
        self_where = " WHERE " + " AND ".join(clauses)
    self_df = pd.read_sql_query(
        f"SELECT date, readiness AS readiness_score FROM self_report{self_where}",
        conn,
        params=self_params,
    )
    if not self_df.empty:
        self_df["device"] = "self"
        for m in metrics:
            if m != "readiness_score" and m not in self_df.columns:
                self_df[m] = pd.NA
        # Column order match for clean concat.
        self_df = self_df[["date", "device", *metrics]]
        df = pd.concat([df, self_df], ignore_index=True)

    out: list[SpearmanRow] = []
    for metric in metrics:
        wide = df.pivot(index="date", columns="device", values=metric)
        for device_a, device_b in pairs:
            if device_a not in wide.columns or device_b not in wide.columns:
                out.append(
                    SpearmanRow(metric, device_a, device_b, n=0, rho=None, p=None)
                )
                continue
            paired = wide[[device_a, device_b]].dropna()
            n = len(paired)
            if n < min_n:
                out.append(
                    SpearmanRow(metric, device_a, device_b, n=n, rho=None, p=None)
                )
                continue
            rho, p = stats.spearmanr(paired[device_a], paired[device_b])
            out.append(
                SpearmanRow(
                    metric=metric,
                    device_a=device_a,
                    device_b=device_b,
                    n=n,
                    rho=float(rho),
                    p=float(p),
                )
            )
    return out


def format_table(rows: list[SpearmanRow]) -> str:
    if not rows:
        return "(no data)"
    headers = ("metric", "device_a", "device_b", "n", "rho", "p")
    formatted = [
        (
            r.metric,
            r.device_a,
            r.device_b,
            str(r.n),
            f"{r.rho:+.3f}" if r.rho is not None else "—",
            f"{r.p:.4f}" if r.p is not None else "—",
        )
        for r in rows
    ]
    widths = [
        max(len(h), max(len(row[i]) for row in formatted)) for i, h in enumerate(headers)
    ]
    sep = "  "
    out = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append(sep.join("-" * w for w in widths))
    out.extend(sep.join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in formatted)
    return "\n".join(out)


def write_csv(rows: list[SpearmanRow], path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(("metric", "device_a", "device_b", "n", "rho", "p"))
        for r in rows:
            writer.writerow(
                (r.metric, r.device_a, r.device_b, r.n, r.rho, r.p)
            )


def write_json(rows: list[SpearmanRow], path) -> None:
    with open(path, "w") as f:
        json.dump([asdict(r) for r in rows], f, indent=2)
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
uv run pytest tests/test_analysis.py -v -k "self_pair or manual_readiness_fills"
```

Expected: 3 PASS.

- [ ] **Step 5: Run the full analysis test file to confirm no regressions**

```bash
uv run pytest tests/test_analysis.py -v
```

Expected: all existing tests still pass — the COALESCE change is invisible when `manual_metrics` is empty, and the `self` pairs are additive (they appear in the output but don't affect existing pair rows).

- [ ] **Step 6: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all pass.

---

## Task 6: Document `wearable log` in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the "Daily morning convention" section**

In `/Users/xinruiyi/Documents/wearable_devices_datapipeline/CLAUDE.md`, find the section that begins:

```
## Daily morning convention

When the user opens a session in this directory and says **"pull data"**, ...
```

Insert a new item as step 0 **before** the current step 1 (so the numbering shifts: old 1 → new 2, etc.). The full updated section header and steps 0–1 should read:

```markdown
## Daily morning convention

When the user opens a session in this directory and says **"pull data"**, **"morning pull"**, **"fetch yesterday"**, or any obvious variation, do all of the following in one shot:

0. **First, remind the user to run `uv run wearable log` if they haven't already today.** The subjective 1–10 readiness must be entered *before* any device data is shown, otherwise the rating is anchored on what the wearables already concluded. If the user mentions they've already logged, skip this. If they want to log inline, also offer to enter today's Fitbit readiness from the phone app via `uv run wearable log <score> --fitbit-readiness <0-100>` (Google Health's v4 API doesn't return it).
1. Run `uv run wearable pull --date today` **and then** `uv run wearable pull --date yesterday`. Both are required:
```

- [ ] **Step 2: Add a `wearable log` reference to the commands list**

In the `## Commands` block, find the existing line:
```
uv run wearable init                             # create the SQLite db and apply pending migrations
```

Insert the following two lines **immediately after** that line:
```
uv run wearable log                              # interactive: prompt for 1–10 readiness + optional Fitbit readiness
uv run wearable log 7 --fitbit-readiness 48      # one-shot: log subjective=7 and manual Fitbit readiness=48
```

- [ ] **Step 3: No tests for documentation — verify by re-reading the file**

```bash
grep -n "wearable log" /Users/xinruiyi/Documents/wearable_devices_datapipeline/CLAUDE.md
```

Expected: at least 3 matches (commands block × 2, daily morning convention × 1+).

---

## Task 7: End-to-end smoke verification

**Files:**
- None (read-only verification step).

- [ ] **Step 1: Run the full test suite**

```bash
cd /Users/xinruiyi/Documents/wearable_devices_datapipeline
uv run pytest -q
```

Expected: every test passes.

- [ ] **Step 2: Apply the new migration to the real database**

```bash
uv run wearable init
```

Expected output: `Applied migrations: 0002_self_report_and_manual_metrics` (on first run) or `Database already up to date.` (on subsequent runs).

- [ ] **Step 3: Smoke-test the CLI non-interactively**

```bash
uv run wearable log 7 --fitbit-readiness 48 --date 2026-06-22 --no-prompt
```

Expected output:
```
Logged: self=7, google_health.readiness_score=48 for 2026-06-22
```

Verify it actually wrote:
```bash
sqlite3 data/wearable.db "SELECT * FROM self_report WHERE date='2026-06-22'; SELECT * FROM manual_metrics WHERE date='2026-06-22';"
```

Expected: one row in each table.

- [ ] **Step 4: Smoke-test invalid input**

```bash
uv run wearable log 11 --no-prompt
echo "exit code: $?"
```

Expected: error message about range, exit code non-zero (≥1), and no new row in `self_report`.

- [ ] **Step 5: Smoke-test interactive mode**

```bash
echo -e "8\n55\n" | uv run wearable log --date 2026-06-23
```

Expected: prompts visible in output, then a `Logged: self=8, google_health.readiness_score=55 for 2026-06-23` confirmation. Then:

```bash
sqlite3 data/wearable.db "SELECT * FROM self_report WHERE date='2026-06-23'; SELECT * FROM manual_metrics WHERE date='2026-06-23';"
```

Expected: one row in each table for `2026-06-23`.

- [ ] **Step 6: Clean up the smoke-test rows (optional)**

If the user wants the smoke-test rows kept (they're real-ish entries), leave them. Otherwise:
```bash
sqlite3 data/wearable.db "DELETE FROM self_report WHERE date IN ('2026-06-22', '2026-06-23'); DELETE FROM manual_metrics WHERE date IN ('2026-06-22', '2026-06-23');"
```

Ask the user before deleting.

- [ ] **Step 7: Final summary**

Report back:
- Number of tests passing.
- That migration `0002_self_report_and_manual_metrics` applied cleanly.
- That `wearable log` round-trips data correctly in both modes.
- Any rows left behind from smoke testing (so the user can decide whether to keep or delete).

---

## Done

When all tasks above are checked off, the feature is complete:
- `wearable log` records subjective 1–10 readiness + optional manual Fitbit readiness.
- `wearable analyze` correlates the subjective score against each device on `readiness_score` and uses manual Fitbit readiness to restore the `(oura, google_health)` and `(whoop, google_health)` readiness pairs.
- The morning ritual is documented in `CLAUDE.md`.
- No dashboard changes (deliberately out of scope; can be added later as a separate ticket).
