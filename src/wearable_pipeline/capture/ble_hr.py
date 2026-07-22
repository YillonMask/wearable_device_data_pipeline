from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

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


Sample = tuple[str, str, int, int]  # (device, ts_utc_iso, t_offset_ms, bpm)


def make_notification_handler(
    device: str,
    session_start: datetime,
    on_sample: Callable[[Sample], None],
) -> Callable[[Any, bytearray], None]:
    """Build a bleak notification callback that decodes and dispatches a Sample."""

    def handler(_char: Any, data: bytearray) -> None:
        now = datetime.now(timezone.utc)
        bpm = parse_hr_measurement(bytes(data))
        offset_ms = int((now - session_start).total_seconds() * 1000)
        on_sample((device, now.isoformat(), max(offset_ms, 0), bpm))

    return handler


async def scan_hr_peripherals(timeout: float = 8.0) -> list[tuple[str, str]]:
    """Discover nearby BLE peripherals advertising the Heart Rate service."""
    from bleak import BleakScanner

    devices = await BleakScanner.discover(
        timeout=timeout, service_uuids=[HR_SERVICE_UUID]
    )
    return [(d.name or "(unknown)", d.address) for d in devices]


async def capture_session(
    addresses: dict[str, str],
    session_start: datetime,
    on_sample: Callable[[Sample], None],
    stop_event: asyncio.Event,
    *,
    client_factory: Callable[[str], Any] | None = None,
    reconnect_attempts: int = 3,
) -> dict[str, int]:
    """Connect each device, forward samples via on_sample until stop_event set.

    Returns a per-device count of samples received. A device that keeps failing
    to connect is logged and skipped; the others continue (partial tolerance).
    """
    if client_factory is None:
        from bleak import BleakClient

        client_factory = BleakClient

    counts: dict[str, int] = {d: 0 for d in addresses}

    def counting_sink(device: str):
        base = make_notification_handler(device, session_start, on_sample)

        def wrapped(char: Any, data: bytearray) -> None:
            counts[device] += 1
            base(char, data)

        return wrapped

    async def run_device(device: str, address: str) -> None:
        for attempt in range(1, reconnect_attempts + 1):
            try:
                async with client_factory(address) as client:
                    await client.start_notify(
                        HR_MEASUREMENT_UUID, counting_sink(device)
                    )
                    await stop_event.wait()
                    await client.stop_notify(HR_MEASUREMENT_UUID)
                return
            except Exception as exc:
                logger.warning(
                    "device %s connect/notify failed (attempt %d/%d): %s",
                    device, attempt, reconnect_attempts, exc,
                )
                if stop_event.is_set():
                    return
                await asyncio.sleep(1.0)
        logger.error("device %s gave up after %d attempts", device, reconnect_attempts)

    await asyncio.gather(*(run_device(d, a) for d, a in addresses.items()))
    return counts
