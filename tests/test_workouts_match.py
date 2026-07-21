from datetime import date, datetime, timezone

from wearable_pipeline.models import Workout
from wearable_pipeline.workouts import match_workouts


def _w(device, pid, start_h, end_h):
    return Workout(
        device=device, provider_id=pid,
        start_time=datetime(2026, 6, 24, start_h, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 24, end_h, tzinfo=timezone.utc),
        date=date(2026, 6, 24),
    )


def test_overlapping_sessions_pair():
    whoop = [_w("whoop", "a", 20, 21)]
    google = [_w("google_health", "x", 20, 21)]
    res = match_workouts(whoop, google)
    assert len(res.pairs) == 1
    assert res.pairs[0].whoop.provider_id == "a"
    assert res.pairs[0].google.provider_id == "x"
    assert not res.unmatched_whoop and not res.unmatched_google


def test_disjoint_sessions_stay_unmatched():
    whoop = [_w("whoop", "a", 6, 7)]
    google = [_w("google_health", "x", 20, 21)]
    res = match_workouts(whoop, google)
    assert res.pairs == []
    assert [w.provider_id for w in res.unmatched_whoop] == ["a"]
    assert [g.provider_id for g in res.unmatched_google] == ["x"]


def test_each_session_matched_at_most_once():
    # One whoop session overlaps two google sessions; only the best-overlap wins.
    whoop = [_w("whoop", "a", 20, 22)]
    google = [_w("google_health", "x", 20, 22), _w("google_health", "y", 21, 22)]
    res = match_workouts(whoop, google)
    assert len(res.pairs) == 1
    assert res.pairs[0].google.provider_id == "x"  # larger overlap (2h vs 1h)
    assert [g.provider_id for g in res.unmatched_google] == ["y"]


def test_empty_inputs():
    res = match_workouts([], [])
    assert res.pairs == [] and res.unmatched_whoop == [] and res.unmatched_google == []
