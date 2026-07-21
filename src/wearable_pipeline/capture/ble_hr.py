from __future__ import annotations

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


def parse_hr_measurement(data: bytes) -> int:
    """Decode BPM from a BLE Heart Rate Measurement (0x2A37) payload.

    Byte 0 is a flags field; bit 0 selects the HR value format:
    0 -> uint8 in byte 1, 1 -> uint16 little-endian in bytes 1..2.
    Any trailing fields (energy expended, RR-intervals) are ignored.
    """
    if len(data) < 2:
        raise ValueError(f"HR measurement payload too short: {data!r}")
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            raise ValueError(f"16-bit HR flagged but payload too short: {data!r}")
        return int.from_bytes(data[1:3], "little")
    return data[1]
