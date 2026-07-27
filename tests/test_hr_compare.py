from __future__ import annotations

import math

from wearable_pipeline.capture.compare import (
    align_series,
    compute_stats,
    series_rates,
)


def _samples(fitbit_every: int = 3):
    """Whoop at 1 Hz for 10 s; Fitbit (google_health) every `fitbit_every` s."""
    out = []
    for sec in range(10):
        out.append({"device": "whoop", "ts_utc": "x", "t_offset_ms": sec * 1000, "bpm": 120 + sec})
    for sec in range(0, 10, fitbit_every):
        out.append({"device": "google_health", "ts_utc": "x", "t_offset_ms": sec * 1000, "bpm": 118 + sec})
    return out


def test_align_step_fills_sparse_series():
    aligned = align_series(_samples())  # fitbit every 3 s
    assert set(aligned.columns) == {"whoop", "google_health"}
    assert len(aligned) == 10
    # Fitbit is sparse but step-held -> no gaps across the 10 s, and held values repeat
    assert aligned["google_health"].notna().all()
    assert aligned["google_health"].iloc[1] == aligned["google_health"].iloc[0]  # held


def test_series_rates_report_density():
    rates = series_rates(_samples())
    assert rates["whoop"]["n"] == 10
    assert rates["whoop"]["effective_hz"] == 1.0
    assert rates["whoop"]["median_gap_s"] == 1.0
    assert rates["google_health"]["n"] == 4
    assert rates["google_health"]["median_gap_s"] == 3.0


def test_stats_over_step_filled_grid():
    samples = _samples()  # fitbit every 3 s
    stats = compute_stats(align_series(samples), series_rates(samples))
    assert stats["n_overlap"] == 10
    # whoop 120..129 vs fitbit held 118/121/124/127 -> |diff| cycles 2,3,4
    assert math.isclose(stats["mean_abs_diff"], 2.9, abs_tol=1e-9)
    assert stats["max_abs_diff"] == 4.0
    assert stats["pct_within_5"] == 100.0
    # rate fields threaded into per_device
    assert stats["per_device"]["google_health"]["effective_hz"] == round(3 / 9.0, 2)
    assert stats["per_device"]["whoop"]["n_samples"] == 10


def _baseline_samples():
    """Strap baseline + Whoop (offset +1) + Fitbit (offset -3), all 1 Hz, 10 s."""
    out = []
    for sec in range(10):
        out.append({"device": "strap", "ts_utc": "x", "t_offset_ms": sec * 1000, "bpm": 120 + sec})
        out.append({"device": "whoop", "ts_utc": "x", "t_offset_ms": sec * 1000, "bpm": 121 + sec})
        out.append({"device": "google_health", "ts_utc": "x", "t_offset_ms": sec * 1000, "bpm": 117 + sec})
    return out


def test_baseline_reports_each_device_vs_strap():
    samples = _baseline_samples()
    stats = compute_stats(align_series(samples), series_rates(samples), baseline="strap")
    assert stats["baseline"] == "strap"
    # one pair per non-baseline device; strap is not compared against itself
    assert set(stats["pairs"]) == {"whoop", "google_health"}
    assert stats["pairs"]["whoop"]["mean_abs_diff"] == 1.0   # constant +1 vs strap
    assert stats["pairs"]["google_health"]["mean_abs_diff"] == 3.0  # constant -3
    assert stats["pairs"]["whoop"]["pct_within_5"] == 100.0
    assert stats["pairs"]["whoop"]["n_overlap"] == 10
    # per_device summaries still present for all three
    assert set(stats["per_device"]) == {"strap", "whoop", "google_health"}
    # legacy top-level agreement keys are absent in baseline mode
    assert "mean_abs_diff" not in stats


def test_baseline_absent_falls_back_to_legacy_pair():
    # baseline=None keeps the two-device top-level agreement shape
    samples = _samples()
    stats = compute_stats(align_series(samples), series_rates(samples), baseline=None)
    assert "pairs" not in stats
    assert "mean_abs_diff" in stats


def test_max_hold_expires_into_gap():
    # Fitbit sample only at t=0; with a 3 s hold it should NaN out after sec 3.
    samples = [{"device": "whoop", "ts_utc": "x", "t_offset_ms": s * 1000, "bpm": 120 + s} for s in range(10)]
    samples.append({"device": "google_health", "ts_utc": "x", "t_offset_ms": 0, "bpm": 118})
    aligned = align_series(samples, max_hold_s=3)
    stats = compute_stats(aligned)
    assert stats["n_overlap"] == 4  # seconds 0,1,2,3 held; 4..9 are gaps
    assert stats["pearson_r"] is None   # constant Fitbit segment -> undefined
    assert stats["spearman_rho"] is None
