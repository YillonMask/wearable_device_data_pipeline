from __future__ import annotations

import pytest

from wearable_pipeline.capture.ble_hr import parse_hr_measurement


def test_uint8_format():
    # flags=0x00 -> 8-bit HR in byte 1
    assert parse_hr_measurement(bytes([0x00, 72])) == 72


def test_uint16_format():
    # flags=0x01 -> 16-bit little-endian HR in bytes 1..2 (300 = 0x012C)
    assert parse_hr_measurement(bytes([0x01, 0x2C, 0x01])) == 300


def test_ignores_trailing_rr_and_energy_bytes():
    # flags=0x00, HR=65, then two trailing RR-interval bytes -> still 65
    assert parse_hr_measurement(bytes([0x00, 65, 0x00, 0x02])) == 65


def test_empty_payload_raises():
    with pytest.raises(ValueError):
        parse_hr_measurement(b"")
