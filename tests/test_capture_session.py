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


class AlwaysFailClient:
    """BLE client whose connection always fails on enter."""

    def __init__(self, address: str):
        self.address = address

    async def __aenter__(self):
        raise RuntimeError("connect failed")

    async def __aexit__(self, *exc):
        return False


def test_one_device_fails_other_continues():
    start = datetime(2026, 7, 21, 18, 30, 0, tzinfo=timezone.utc)
    got = []
    stop = asyncio.Event()

    def factory(addr):
        return FakeClient(addr) if addr == "A" else AlwaysFailClient(addr)

    async def scenario():
        async def stopper():
            await asyncio.sleep(0.05)
            stop.set()

        counts, _ = await asyncio.gather(
            capture_session(
                {"whoop": "A", "google_health": "B"},
                start,
                got.append,
                stop,
                client_factory=factory,
                reconnect_attempts=2,
            ),
            stopper(),
        )
        return counts

    counts = asyncio.run(scenario())
    assert counts["whoop"] == 3          # healthy device streamed
    assert counts["google_health"] == 0  # failing device present but zero samples
    assert {s[0] for s in got} == {"whoop"}


def test_device_reconnects_after_transient_failure():
    start = datetime(2026, 7, 21, 18, 30, 0, tzinfo=timezone.utc)
    got = []
    stop = asyncio.Event()
    state = {"fails": 1}  # fail once, then succeed

    class FlakyClient:
        def __init__(self, address: str):
            self.address = address

        async def __aenter__(self):
            if state["fails"] > 0:
                state["fails"] -= 1
                raise RuntimeError("transient")
            return self

        async def __aexit__(self, *exc):
            return False

        async def start_notify(self, uuid, handler):
            handler(None, bytearray([0x00, 105]))
            await asyncio.sleep(0)

        async def stop_notify(self, uuid):
            return None

    async def scenario():
        async def stopper():
            await asyncio.sleep(1.3)  # after the 1.0s retry backoff
            stop.set()

        counts, _ = await asyncio.gather(
            capture_session(
                {"whoop": "A"},
                start,
                got.append,
                stop,
                client_factory=lambda addr: FlakyClient(addr),
                reconnect_attempts=3,
            ),
            stopper(),
        )
        return counts

    counts = asyncio.run(scenario())
    assert counts["whoop"] == 1  # reconnected on 2nd attempt, got one sample
