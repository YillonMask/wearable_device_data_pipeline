from __future__ import annotations

from typer.testing import CliRunner

from wearable_pipeline import cli

runner = CliRunner()


def test_hr_scan_lists_devices(monkeypatch):
    async def fake_scan(timeout: float = 8.0):
        return [("WHOOP", "AA:BB:CC:DD:EE:FF"), ("Fitbit Air", "11:22:33:44:55:66")]

    monkeypatch.setattr(cli, "scan_hr_peripherals", fake_scan)
    result = runner.invoke(cli.app, ["hr-scan"])
    assert result.exit_code == 0
    assert "AA:BB:CC:DD:EE:FF" in result.output
    assert "Fitbit Air" in result.output


def test_hr_scan_missing_bleak(monkeypatch):
    def boom(timeout: float = 8.0):
        raise ImportError("no bleak")

    monkeypatch.setattr(cli, "scan_hr_peripherals", boom)
    result = runner.invoke(cli.app, ["hr-scan"])
    assert result.exit_code == 2
    assert "uv sync --extra ble" in result.output
