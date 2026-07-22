from __future__ import annotations

import statistics
from typing import Any


def series_rates(samples: list[dict]) -> dict:
    """Per-device sampling density: {device: {n, effective_hz, median_gap_s}}."""
    rates: dict[str, dict] = {}
    devices: list[str] = []
    for s in samples:
        if s["device"] not in devices:
            devices.append(s["device"])
    for device in devices:
        offs = sorted(s["t_offset_ms"] for s in samples if s["device"] == device)
        entry: dict = {"n": len(offs), "effective_hz": None, "median_gap_s": None}
        if len(offs) >= 2:
            span_s = (offs[-1] - offs[0]) / 1000.0
            gaps = [(b - a) / 1000.0 for a, b in zip(offs, offs[1:])]
            entry["effective_hz"] = round((len(offs) - 1) / span_s, 2) if span_s > 0 else None
            entry["median_gap_s"] = round(statistics.median(gaps), 2)
        rates[device] = entry
    return rates


def align_series(
    samples: list[dict], *, step_s: int = 1, max_hold_s: int = 8
) -> Any:
    """Resample each device onto a shared 1 s grid by step interpolation.

    Each device's last observed BPM is held forward across the grid for up to
    max_hold_s seconds (so a sparse, contact-gated series like Fitbit Air draws
    as a step function), after which the cell is NaN until the next sample.
    Seconds before a device's first sample are NaN. Columns are device names,
    index is the whole-second offset.
    """
    import pandas as pd

    if not samples:
        return pd.DataFrame()

    max_s = max(s["t_offset_ms"] for s in samples) // 1000
    grid = list(range(0, max_s + 1, step_s))
    devices: list[str] = []
    for s in samples:
        if s["device"] not in devices:
            devices.append(s["device"])

    data: dict[str, list[float]] = {}
    for device in devices:
        pts = sorted(
            (s["t_offset_ms"] / 1000.0, float(s["bpm"]))
            for s in samples
            if s["device"] == device
        )
        col: list[float] = []
        idx = 0
        last_t: float | None = None
        last_val = float("nan")
        for sec in grid:
            while idx < len(pts) and pts[idx][0] <= sec:
                last_t, last_val = pts[idx]
                idx += 1
            if last_t is not None and (sec - last_t) <= max_hold_s:
                col.append(last_val)
            else:
                col.append(float("nan"))
        data[device] = col
    return pd.DataFrame(data, index=grid)


def compute_stats(aligned: Any, rates: dict | None = None) -> dict:
    from scipy.stats import pearsonr, spearmanr

    devices = list(aligned.columns)
    per_device = {}
    for d in devices:
        col = aligned[d].dropna()
        entry: dict = {
            "avg": round(float(col.mean()), 1) if len(col) else None,
            "max": int(col.max()) if len(col) else None,
            "min": int(col.min()) if len(col) else None,
        }
        if rates and d in rates:
            entry["effective_hz"] = rates[d]["effective_hz"]
            entry["median_gap_s"] = rates[d]["median_gap_s"]
            entry["n_samples"] = rates[d]["n"]
        per_device[d] = entry

    stats: dict = {"per_device": per_device, "n_overlap": 0}
    if len(devices) == 2:
        both = aligned[devices].dropna()
        stats["n_overlap"] = int(len(both))
        if len(both) >= 2:
            a, b = both[devices[0]], both[devices[1]]
            diff = (a - b).abs()
            stats["mean_abs_diff"] = round(float(diff.mean()), 2)
            stats["median_abs_diff"] = round(float(diff.median()), 2)
            stats["max_abs_diff"] = round(float(diff.max()), 2)
            stats["pct_within_5"] = round(float((diff <= 5).mean() * 100), 1)
            # Correlation is undefined if either series is constant over the
            # overlap (e.g. a flat step-held Fitbit segment) -> report None
            # rather than emit scipy's ConstantInputWarning and a NaN.
            if a.nunique() > 1 and b.nunique() > 1:
                stats["pearson_r"] = round(float(pearsonr(a, b)[0]), 3)
                stats["spearman_rho"] = round(float(spearmanr(a, b)[0]), 3)
            else:
                stats["pearson_r"] = None
                stats["spearman_rho"] = None
    return stats
