from __future__ import annotations

from typer.testing import CliRunner

from wearable_pipeline import cli, db
from wearable_pipeline.storage import create_hr_session, insert_hr_samples

runner = CliRunner()


def _seed(conn):
    create_hr_session(
        conn, session_id="S1", label="bike",
        started_at="2026-07-21T18:30:05+00:00",
        devices=["whoop", "google_health"],
    )
    rows = []
    for sec in range(10):
        # ts_utc must be unique per (session_id, device) — it's part of the
        # hr_samples primary key (migration 0005) — so give each row its own
        # receipt timestamp rather than reusing a constant placeholder.
        rows.append(("S1", "whoop", f"2026-07-21T18:30:{5 + sec:02d}+00:00", sec * 1000, 120 + sec))
    # Fitbit sparse (every 3 s) to exercise the step-fill + rate reporting path
    for sec in range(0, 10, 3):
        rows.append(("S1", "google_health", f"2026-07-21T18:30:{5 + sec:02d}+00:00", sec * 1000, 118 + sec))
    insert_hr_samples(conn, rows)


def test_hr_compare_prints_stats_and_writes_png(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    from wearable_pipeline import config
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    _seed(conn)
    conn.close()

    out = tmp_path / "chart.png"
    result = runner.invoke(cli.app, ["hr-compare", "S1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "mean_abs_diff" in result.output
    assert "rate=" in result.output  # per-device effective Hz surfaced
    assert out.exists() and out.stat().st_size > 0
