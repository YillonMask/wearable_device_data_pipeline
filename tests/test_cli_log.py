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
    result = runner.invoke(app, ["log", "101", "--no-prompt"])
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
    from wearable_pipeline.storage import upsert_self_report

    conn = db.connect(db_env)
    upsert_self_report(conn, day=date(2026, 6, 21), readiness=3)
    conn.close()

    result = runner.invoke(
        app, ["log", "7", "--date", "2026-06-22", "--no-prompt"]
    )
    assert result.exit_code == 0
    assert "2026-06-21" not in result.output
