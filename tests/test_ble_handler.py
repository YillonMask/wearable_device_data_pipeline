from __future__ import annotations

from datetime import datetime, timezone

from wearable_pipeline.capture.ble_hr import make_notification_handler


def test_handler_emits_sample_with_offset():
    start = datetime(2026, 7, 21, 18, 30, 0, tzinfo=timezone.utc)
    got = []
    handler = make_notification_handler("whoop", start, got.append)

    # offset is derived from datetime.now(); start is in the past so it's positive
    handler(None, bytearray([0x00, 130]))
    handler(None, bytearray([0x00, 131]))

    assert [s[0] for s in got] == ["whoop", "whoop"]
    assert [s[3] for s in got] == [130, 131]
    # ts is ISO UTC, offset is a non-negative int
    assert got[0][1].endswith("+00:00")
    assert isinstance(got[0][2], int) and got[0][2] >= 0
