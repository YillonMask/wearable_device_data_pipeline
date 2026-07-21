from __future__ import annotations


def test_ble_addresses_loaded_from_env(monkeypatch):
    monkeypatch.setenv("WHOOP_BLE_ADDRESS", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("FITBIT_BLE_ADDRESS", "11:22:33:44:55:66")
    from wearable_pipeline import config
    s = config.load_settings()
    assert s.whoop_ble_address == "AA:BB:CC:DD:EE:FF"
    assert s.fitbit_ble_address == "11:22:33:44:55:66"


def test_ble_addresses_default_none(monkeypatch):
    monkeypatch.delenv("WHOOP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("FITBIT_BLE_ADDRESS", raising=False)
    from wearable_pipeline import config
    s = config.load_settings()
    assert s.whoop_ble_address is None
    assert s.fitbit_ble_address is None
