from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from wearable_pipeline import cli

runner = CliRunner()


def test_hr_capture_missing_addresses(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("WHOOP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("FITBIT_BLE_ADDRESS", raising=False)
    import importlib
    from wearable_pipeline import config
    importlib.reload(config)
    # `reload` re-runs `load_dotenv`, which re-populates os.environ from the
    # real project `.env` for any var not already set — undoing the delenv
    # above if this machine's .env happens to define these. Delenv again
    # post-reload so the test is isolated from the real .env's contents.
    monkeypatch.delenv("WHOOP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("FITBIT_BLE_ADDRESS", raising=False)
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    result = runner.invoke(cli.app, ["hr-capture", "--minutes", "0"])
    assert result.exit_code == 2
    assert "hr-scan" in result.output


def test_hr_capture_records_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WHOOP_BLE_ADDRESS", "A")
    monkeypatch.setenv("FITBIT_BLE_ADDRESS", "B")
    import importlib
    from wearable_pipeline import config
    importlib.reload(config)
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    async def fake_capture(addresses, start, on_sample, stop_event, **kw):
        on_sample(("whoop", start.isoformat(), 1000, 120))
        on_sample(("google_health", start.isoformat(), 1000, 118))
        return {"whoop": 1, "google_health": 1}

    monkeypatch.setattr(cli, "capture_session", fake_capture)

    result = runner.invoke(cli.app, ["hr-capture", "--label", "bike", "--minutes", "0"])
    assert result.exit_code == 0, result.output
    assert "bike" in result.output

    # verify rows landed
    from wearable_pipeline import db
    conn = db.connect(tmp_path / "t.db")
    n = conn.execute("SELECT COUNT(*) FROM hr_samples").fetchone()[0]
    assert n == 2
