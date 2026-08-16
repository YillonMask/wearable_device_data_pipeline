from __future__ import annotations

from datetime import date, datetime, timezone

from wearable_pipeline.clients.garmin import (
    GarminClient,
    _chart_size,
    _descriptor_index,
    _parse_details,
    activity_start_utc,
)

START = datetime(2026, 8, 11, 4, 10, 0, tzinfo=timezone.utc)


class FakeApi:
    """Stands in for garminconnect.Garmin (which needs the optional extra)."""

    def __init__(self, details=None, activities=None):
        self._details = details or {}
        self._activities = activities or []
        self.detail_calls: list[tuple] = []
        self.date_calls: list[tuple] = []

    def get_activity_details(self, activity_id, maxchart=2000, maxpoly=4000):
        self.detail_calls.append((activity_id, maxchart, maxpoly))
        return self._details

    def get_activities_by_date(self, startdate, enddate=None, **kw):
        self.date_calls.append((startdate, enddate))
        return self._activities


def _details(n: int = 5, hr_index: int = 1, ts_index: int = 0) -> dict:
    base_ms = int(START.timestamp() * 1000)
    descriptors = [
        {"key": "directTimestamp", "metricsIndex": ts_index},
        {"key": "directHeartRate", "metricsIndex": hr_index},
    ]
    metrics = []
    for i in range(n):
        row = [None, None]
        row[ts_index] = base_ms + i * 1000
        row[hr_index] = 120 + i
        metrics.append({"metrics": row})
    return {"metricDescriptors": descriptors, "activityDetailMetrics": metrics}


# --- positional metric resolution -------------------------------------------


def test_parse_details_resolves_positional_metrics() -> None:
    samples = _parse_details(_details())
    assert [bpm for _, bpm in samples] == [120, 121, 122, 123, 124]
    assert samples[0][0] == START
    # 1 s spacing — Garmin has no server-side bucketing
    assert (samples[1][0] - samples[0][0]).total_seconds() == 1


def test_parse_details_honours_a_different_index_layout() -> None:
    """Layout varies by device/activity type, so indices must never be assumed."""
    samples = _parse_details(_details(hr_index=0, ts_index=1))
    assert [bpm for _, bpm in samples] == [120, 121, 122, 123, 124]


def test_descriptor_index_prefers_exact_key_over_substring() -> None:
    details = {
        "metricDescriptors": [
            {"key": "directHeartRateZone", "metricsIndex": 9},
            {"key": "directHeartRate", "metricsIndex": 2},
        ]
    }
    assert _descriptor_index(details, "directHeartRate", "heartrate") == 2


def test_descriptor_index_falls_back_to_substring() -> None:
    details = {"metricDescriptors": [{"key": "weirdHeartRateThing", "metricsIndex": 4}]}
    assert _descriptor_index(details, "directHeartRate", "heartrate") == 4


def test_descriptor_index_ignores_malformed_descriptors() -> None:
    details = {
        "metricDescriptors": [
            {"key": "directHeartRate", "metricsIndex": -1},
            {"key": "directHeartRate", "metricsIndex": True},
            {"key": None, "metricsIndex": 3},
        ]
    }
    assert _descriptor_index(details, "directHeartRate", "heartrate") is None


# --- gaps and hand-entered activities ---------------------------------------


def test_parse_details_drops_null_and_zero_hr_samples() -> None:
    base_ms = int(START.timestamp() * 1000)
    details = {
        "metricDescriptors": [
            {"key": "directTimestamp", "metricsIndex": 0},
            {"key": "directHeartRate", "metricsIndex": 1},
        ],
        "activityDetailMetrics": [
            {"metrics": [base_ms, 120]},
            {"metrics": [base_ms + 1000, None]},  # strap lost contact
            {"metrics": [base_ms + 2000, 0]},  # sentinel, not a real reading
            {"metrics": [base_ms + 3000]},  # channel missing entirely
            {"metrics": [base_ms + 4000, 124]},
        ],
    }
    assert [bpm for _, bpm in _parse_details(details)] == [120, 124]


def test_parse_details_hand_entered_activity_returns_empty() -> None:
    """No HR descriptor at all — a legitimate answer, not an error."""
    details = {
        "metricDescriptors": [{"key": "directTimestamp", "metricsIndex": 0}],
        "activityDetailMetrics": [{"metrics": [1]}],
    }
    assert _parse_details(details) == []
    assert _parse_details({}) == []


def test_parse_details_sorts_by_timestamp() -> None:
    base_ms = int(START.timestamp() * 1000)
    details = {
        "metricDescriptors": [
            {"key": "directTimestamp", "metricsIndex": 0},
            {"key": "directHeartRate", "metricsIndex": 1},
        ],
        "activityDetailMetrics": [
            {"metrics": [base_ms + 1000, 121]},
            {"metrics": [base_ms, 120]},
        ],
    }
    assert [bpm for _, bpm in _parse_details(details)] == [120, 121]


# --- maxChartSize sizing -----------------------------------------------------


def test_chart_size_scales_with_duration_to_avoid_silent_downsampling() -> None:
    assert _chart_size(None) == 2000
    assert _chart_size(0) == 2000
    assert _chart_size(1000) == 2000  # floor
    assert _chart_size(10000) == 12500  # 1.25x headroom over 1 Hz
    assert _chart_size(10**9) == 60000  # capped


def test_fetch_activity_hr_sizes_chart_and_skips_polyline() -> None:
    api = FakeApi(details=_details())
    client = GarminClient(api=api)

    series = client.fetch_activity_hr("999", duration_s=10000)

    assert [bpm for _, bpm in series.samples] == [120, 121, 122, 123, 124]
    assert api.detail_calls == [("999", 12500, 0)]
    assert series.raw.payload == api._details
    assert "999" in series.raw.endpoint


def test_list_activities_defaults_end_to_start() -> None:
    api = FakeApi(activities=[{"activityId": 1}])
    client = GarminClient(api=api)

    client.list_activities(date(2026, 8, 10))
    client.list_activities(date(2026, 8, 10), date(2026, 8, 12))

    assert api.date_calls == [("2026-08-10", "2026-08-10"), ("2026-08-10", "2026-08-12")]


# --- activity summary parsing ------------------------------------------------


def test_activity_start_utc_parses_space_separated_gmt() -> None:
    assert activity_start_utc({"startTimeGMT": "2026-08-11 04:10:00"}) == START


def test_activity_start_utc_handles_missing_or_bad_values() -> None:
    assert activity_start_utc({}) is None
    assert activity_start_utc({"startTimeGMT": "nonsense"}) is None
    assert activity_start_utc({"startTimeGMT": None}) is None
