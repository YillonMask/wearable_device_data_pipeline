from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from wearable_pipeline.capture.ble_hr import capture_session


class FakeClient:
    """Minimal async-context BLE client that emits a few HR notifications."""

    def __init__(self, address: str):
        self.address = address

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def start_notify(self, uuid, handler):
        # emit three uint8 notifications
        for bpm in (100, 101, 102):
            handler(None, bytearray([0x00, bpm]))
            await asyncio.sleep(0)

    async def stop_notify(self, uuid):
        return None


def test_capture_collects_from_both_devices():
    start = datetime(2026, 7, 21, 18, 30, 0, tzinfo=timezone.utc)
    got = []
    stop = asyncio.Event()

    async def scenario():
        # stop shortly after notifications are delivered
        async def stopper():
            await asyncio.sleep(0.05)
            stop.set()

        counts, _ = await asyncio.gather(
            capture_session(
                {"whoop": "A", "google_health": "B"},
                start,
                got.append,
                stop,
                client_factory=FakeClient,
            ),
            stopper(),
        )
        return counts

    counts = asyncio.run(scenario())
    assert counts["whoop"] == 3
    assert counts["google_health"] == 3
    assert {s[0] for s in got} == {"whoop", "google_health"}
