from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from wearable_pipeline import cli

runner = CliRunner()


def test_hr_capture_missing_addresses(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("WHOOP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("FITBIT_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("STRAP_BLE_ADDRESS", raising=False)
    from wearable_pipeline import config
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    result = runner.invoke(cli.app, ["hr-capture", "--minutes", "0"])
    assert result.exit_code == 2
    assert "hr-scan" in result.output


def test_hr_capture_one_address_not_enough(monkeypatch, tmp_path):
    # A single BLE device can't be compared against anything -> refuse.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("STRAP_BLE_ADDRESS", "C")
    monkeypatch.delenv("WHOOP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("FITBIT_BLE_ADDRESS", raising=False)
    from wearable_pipeline import config
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    result = runner.invoke(cli.app, ["hr-capture", "--minutes", "0"])
    assert result.exit_code == 2


def test_hr_capture_records_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WHOOP_BLE_ADDRESS", "A")
    monkeypatch.setenv("FITBIT_BLE_ADDRESS", "B")
    from wearable_pipeline import config
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


def test_hr_capture_includes_strap_baseline(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("STRAP_BLE_ADDRESS", "C")
    monkeypatch.setenv("WHOOP_BLE_ADDRESS", "A")
    monkeypatch.setenv("FITBIT_BLE_ADDRESS", "B")
    from wearable_pipeline import config
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    seen = {}

    async def fake_capture(addresses, start, on_sample, stop_event, **kw):
        seen["addresses"] = dict(addresses)
        on_sample(("strap", start.isoformat(), 1000, 121))
        on_sample(("whoop", start.isoformat(), 1000, 120))
        on_sample(("google_health", start.isoformat(), 1000, 118))
        return {d: 1 for d in addresses}

    monkeypatch.setattr(cli, "capture_session", fake_capture)

    result = runner.invoke(cli.app, ["hr-capture", "--minutes", "0"])
    assert result.exit_code == 0, result.output
    # strap is captured first (reference), alongside both wearables
    assert list(seen["addresses"]) == ["strap", "whoop", "google_health"]

    from wearable_pipeline import db
    conn = db.connect(tmp_path / "t.db")
    devices = {r[0] for r in conn.execute("SELECT DISTINCT device FROM hr_samples")}
    assert devices == {"strap", "whoop", "google_health"}
    stored = conn.execute("SELECT devices FROM hr_sessions").fetchone()[0]
    assert "strap" in stored
