from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wearable_pipeline import db
from wearable_pipeline.models import DailyMetrics
from wearable_pipeline.recovery import (
    FEATURES,
    MIN_FEELING_DAYS,
    RecoveryModel,
    load_frame,
    tune_to_feelings,
)
from wearable_pipeline.storage import upsert_daily_metrics

PRIOR = RecoveryModel(
    intercept=69.0,
    weights={
        "hrv": 11.0,
        "rhr": -1.0,
        "resp": -1.2,
        "skin_temp": 0.0,
        "spo2": 0.3,
        "sleep_perf": 1.9,
        "sleep_dur": 1.5,
        "sleep_debt": -0.5,
        "sleep_consistency": 0.4,
        "disturbances": -0.3,
        "prev_strain": -0.6,
    },
)


def _frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        rng.normal(size=(n, len(FEATURES))),
        columns=list(FEATURES),
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )
    frame["whoop"] = PRIOR.predict(frame) + rng.normal(0, 3, n)
    return frame


def test_baseline_excludes_the_day_itself_and_counts_calendar_days(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    start = date(2026, 1, 1)
    hrv = [48.0 if i % 2 else 52.0 for i in range(30)] + [100.0]
    for i, value in enumerate(hrv):
        upsert_daily_metrics(
            conn,
            DailyMetrics(date=start + timedelta(days=i), device="whoop", hrv_ms=value,
                         strain_or_activity_score=float(i)),
        )
    # Day 31 has no row: day 32's "yesterday's strain" must be missing,
    # not day 30's value carried over from the previous stored row.
    upsert_daily_metrics(
        conn,
        DailyMetrics(date=start + timedelta(days=32), device="whoop", strain_or_activity_score=5.0),
    )

    frame = load_frame(conn)

    prior_30 = np.log(hrv[:30])
    expected = (math.log(100.0) - prior_30.mean()) / prior_30.std(ddof=1)
    assert frame.loc["2026-01-31", "hrv"] == pytest.approx(expected)
    assert math.isnan(frame.loc["2026-02-02", "prev_strain"])


def test_tuning_moves_toward_what_actually_drives_feelings() -> None:
    frame = _frame(120, seed=1)
    rng = np.random.default_rng(2)
    # This person's feelings track sleep and yesterday's strain, barely HRV.
    frame["feeling"] = (
        55 + 3 * frame.hrv + 8 * frame.sleep_perf + 6 * frame.sleep_dur
        - 4 * frame.prev_strain + rng.normal(0, 4, len(frame))
    )

    result = tune_to_feelings(frame, PRIOR)

    w = result.model.weights
    assert w["sleep_perf"] + w["sleep_dur"] > 10
    assert w["hrv"] < 6
    assert w["prev_strain"] < -2
    assert result.personal.mae < result.whoop_score.mae - 2


def test_tuning_keeps_whoop_weights_when_feelings_agree_with_whoop() -> None:
    frame = _frame(120, seed=3)
    rng = np.random.default_rng(4)
    # Same drivers as Whoop on a different personal scale (lower mean).
    frame["feeling"] = PRIOR.predict(frame) - 15 + rng.normal(0, 6, len(frame))

    result = tune_to_feelings(frame, PRIOR)

    assert result.lam >= 100
    for f in FEATURES:
        assert result.model.weights[f] == pytest.approx(PRIOR.weights[f], abs=2.0)
    assert result.model.intercept == pytest.approx(PRIOR.intercept - 15, abs=3.0)


def test_tuning_refuses_too_few_feeling_days() -> None:
    frame = _frame(120, seed=5)
    frame["feeling"] = np.nan
    frame.iloc[: MIN_FEELING_DAYS - 1, frame.columns.get_loc("feeling")] = 60.0
    with pytest.raises(ValueError, match=str(MIN_FEELING_DAYS)):
        tune_to_feelings(frame, PRIOR)


def test_scaling_a_group_scales_each_member_and_rejects_unknown_names() -> None:
    scaled = PRIOR.scaled({"sleep": 2.0, "hrv": 0.5})
    assert scaled.weights["sleep_perf"] == pytest.approx(3.8)
    assert scaled.weights["sleep_dur"] == pytest.approx(3.0)
    assert scaled.weights["hrv"] == pytest.approx(5.5)
    assert scaled.weights["rhr"] == PRIOR.weights["rhr"]
    with pytest.raises(ValueError, match="unknown feature"):
        PRIOR.scaled({"mood": 2.0})
