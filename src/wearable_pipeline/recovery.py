"""Personal recovery model: a transparent replica of Whoop's recovery score,
user-adjustable weights, and a version tuned to the owner's own feelings.

Features (all from Whoop's ``daily_metrics`` row for the day) are expressed in
"SD units vs your own recent normal": each value is z-scored against the
trailing ``BASELINE_DAYS`` days, **excluding the day itself**, so a day never
contributes to its own baseline. HRV is log-transformed first because RMSSD is
right-skewed. ``prev_strain`` is the previous calendar day's strain.

A model is ``score = clip(intercept + sum(weight_f * z_f), 0, 100)``, so each
weight reads as "recovery points per 1 SD above your normal".

- ``fit_replica`` fits the weights to Whoop's own recovery score (OLS). It is a
  linear approximation of a proprietary algorithm, not the algorithm itself.
- ``RecoveryModel.scaled`` multiplies chosen weights (e.g. ``sleep=2``) to try
  your own emphasis.
- ``tune_to_feelings`` fits to ``self_report`` scores with ridge regression
  shrunk *toward the replica*, so with little data it stays Whoop-like and
  only moves as far as the feelings justify. The prior strength ``lam`` reads
  as "the Whoop replica is worth ``lam`` days of feelings". It is chosen by
  walk-forward validation, and every reported accuracy is walk-forward too
  (each day predicted only from days before it), so nothing is scored on data
  it was fitted on.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

BASELINE_DAYS = 30
MIN_BASELINE_DAYS = 14
# Walk-forward needs a training history before it predicts anything.
MIN_TRAIN_DAYS = 14
MIN_FEELING_DAYS = 28

FEATURES: tuple[str, ...] = (
    "hrv",
    "rhr",
    "resp",
    "skin_temp",
    "spo2",
    "sleep_perf",
    "sleep_dur",
    "sleep_debt",
    "sleep_consistency",
    "disturbances",
    "prev_strain",
)

SLEEP_FEATURES: tuple[str, ...] = (
    "sleep_perf",
    "sleep_dur",
    "sleep_debt",
    "sleep_consistency",
    "disturbances",
)

# Names accepted by ``RecoveryModel.scaled``: every feature, plus groups.
GROUPS: dict[str, tuple[str, ...]] = {
    **{f: (f,) for f in FEATURES},
    "sleep": SLEEP_FEATURES,
}

FEATURE_LABELS: dict[str, str] = {
    "hrv": "HRV (log RMSSD)",
    "rhr": "resting HR",
    "resp": "respiratory rate",
    "skin_temp": "skin temp",
    "spo2": "blood oxygen (SpO2)",
    "sleep_perf": "sleep performance",
    "sleep_dur": "sleep duration",
    "sleep_debt": "sleep debt",
    "sleep_consistency": "sleep consistency",
    "disturbances": "sleep disturbances",
    "prev_strain": "yesterday's strain",
}

LAMBDA_GRID: tuple[float, ...] = (1, 3, 10, 30, 100, 300, 1000, math.inf)

_SOURCE_COLUMNS = {
    "hrv": "hrv_ms",
    "rhr": "resting_hr",
    "resp": "respiratory_rate",
    "skin_temp": "skin_temp",
    "spo2": "spo2_pct",
    "sleep_perf": "sleep_score",
    "sleep_dur": "total_sleep_minutes",
    "sleep_debt": "sleep_debt_minutes",
    "sleep_consistency": "sleep_consistency",
    "disturbances": "sleep_disturbances",
    "prev_strain": "strain_or_activity_score",
}


def load_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per calendar day: FEATURES (baseline z-scores), ``whoop``
    (Whoop's recovery score) and ``feeling`` (``self_report``), indexed by date.

    Always the full history: slice the result rather than the query, so every
    day keeps a warmed-up baseline.
    """
    raw = pd.read_sql_query(
        "SELECT date, readiness_score AS whoop, "
        + ", ".join(sorted(set(_SOURCE_COLUMNS.values())))
        + " FROM daily_metrics WHERE device = 'whoop' ORDER BY date",
        conn,
        parse_dates=["date"],
    ).set_index("date")
    feelings = pd.read_sql_query(
        "SELECT date, readiness AS feeling FROM self_report",
        conn,
        parse_dates=["date"],
    ).set_index("date")

    days = pd.DatetimeIndex(raw.index.union(feelings.index))
    if days.empty:
        return pd.DataFrame(columns=[*FEATURES, "whoop", "feeling"])
    # Reindex to a gap-free daily calendar so rolling windows and the
    # previous-day shift mean calendar days, not "previous stored row".
    out = pd.DataFrame(index=pd.date_range(days.min(), days.max(), freq="D"))
    raw = raw.reindex(out.index)

    for feature, column in _SOURCE_COLUMNS.items():
        series = raw[column].astype(float)
        if feature == "hrv":
            series = np.log(series)
        if feature == "prev_strain":
            series = series.shift(1)
        out[feature] = _baseline_z(series)
    out["whoop"] = raw["whoop"].astype(float)
    out["feeling"] = feelings["feeling"].reindex(out.index).astype(float)
    return out


def _baseline_z(series: pd.Series) -> pd.Series:
    window = series.rolling(BASELINE_DAYS, min_periods=MIN_BASELINE_DAYS)
    mean = window.mean().shift(1)
    std = window.std().shift(1)
    return (series - mean) / std.where(std > 0)


@dataclass(frozen=True)
class RecoveryModel:
    intercept: float
    weights: dict[str, float]

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        """Score each row; NaN where any feature is missing."""
        w = np.array([self.weights[f] for f in FEATURES])
        raw = self.intercept + frame[list(FEATURES)].to_numpy(dtype=float) @ w
        return pd.Series(np.clip(raw, 0, 100), index=frame.index)

    def scaled(self, factors: dict[str, float]) -> RecoveryModel:
        """Multiply the weights of named features/groups (see ``GROUPS``)."""
        weights = dict(self.weights)
        for name, factor in factors.items():
            if name not in GROUPS:
                raise ValueError(
                    f"unknown feature {name!r}; choose from {', '.join(GROUPS)}"
                )
            for feature in GROUPS[name]:
                weights[feature] *= factor
        return replace(self, weights=weights)


def complete_rows(frame: pd.DataFrame, target: str) -> pd.DataFrame:
    return frame.dropna(subset=[*FEATURES, target])


def fit_replica(frame: pd.DataFrame) -> RecoveryModel:
    """OLS fit of Whoop's recovery score on the features."""
    rows = complete_rows(frame, "whoop")
    if len(rows) <= len(FEATURES) + 1:
        raise ValueError(
            f"need more than {len(FEATURES) + 1} days with Whoop recovery and "
            f"every feature; have {len(rows)}"
        )
    return _ols(rows[list(FEATURES)].to_numpy(float), rows["whoop"].to_numpy(float))


def _ols(X: np.ndarray, y: np.ndarray) -> RecoveryModel:
    A = np.column_stack([np.ones(len(X)), X])
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    return RecoveryModel(float(coef[0]), dict(zip(FEATURES, map(float, coef[1:]))))


def _shrunk(
    X: np.ndarray, y: np.ndarray, prior: RecoveryModel, lam: float
) -> RecoveryModel:
    """Ridge toward ``prior``'s weights; the intercept is left free so a
    different personal scale (e.g. you average 55, Whoop 70) costs nothing."""
    w0 = np.array([prior.weights[f] for f in FEATURES])
    if math.isinf(lam):
        w = w0
    else:
        xm, ym = X.mean(axis=0), y.mean()
        Xc = X - xm
        w = np.linalg.solve(Xc.T @ Xc + lam * np.eye(len(FEATURES)), Xc.T @ (y - ym) + lam * w0)
    intercept = float(np.mean(y - X @ w))
    return RecoveryModel(intercept, dict(zip(FEATURES, map(float, w))))


def contribution_shares(model: RecoveryModel, frame: pd.DataFrame) -> dict[str, float]:
    """Each feature's share of total influence: |weight| x that feature's SD
    in ``frame``, normalized to sum to 1."""
    rows = frame.dropna(subset=list(FEATURES))
    size = {f: abs(model.weights[f]) * float(rows[f].std()) for f in FEATURES}
    total = sum(size.values()) or 1.0
    return {f: v / total for f, v in size.items()}


@dataclass(frozen=True)
class Accuracy:
    n: int  # days scored (walk-forward: each predicted only from earlier days)
    mae: float
    r2: float
    rank_corr: float


def _accuracy(pred: np.ndarray, actual: np.ndarray) -> Accuracy:
    resid = actual - pred
    ss_tot = float(np.sum((actual - actual.mean()) ** 2))
    rank = pd.Series(pred).rank().corr(pd.Series(actual).rank())
    return Accuracy(
        n=len(actual),
        mae=float(np.mean(np.abs(resid))),
        r2=1 - float(np.sum(resid**2)) / ss_tot if ss_tot else float("nan"),
        rank_corr=float(rank),
    )


def backtest_replica(frame: pd.DataFrame, *, min_train: int = 60) -> Accuracy:
    """Walk-forward accuracy of the replica: day t is predicted by a model
    fitted on days before t only."""
    rows = complete_rows(frame, "whoop")
    X, y = rows[list(FEATURES)].to_numpy(float), rows["whoop"].to_numpy(float)
    if len(y) <= min_train:
        raise ValueError(f"need more than {min_train} complete days; have {len(y)}")
    pred = [
        float(np.clip(_predict(_ols(X[:t], y[:t]), X[t]), 0, 100))
        for t in range(min_train, len(y))
    ]
    return _accuracy(np.array(pred), y[min_train:])


def _predict(model: RecoveryModel, x: np.ndarray) -> float:
    return model.intercept + float(x @ np.array([model.weights[f] for f in FEATURES]))


@dataclass(frozen=True)
class TuneResult:
    n_days: int
    lam: float  # chosen prior strength, in "days of feelings"; inf = keep Whoop's weights
    model: RecoveryModel
    personal: Accuracy  # tuned model, lam re-chosen each step from past days only
    whoop_weights: Accuracy  # replica weights, only the intercept refitted
    whoop_score: Accuracy  # Whoop's own recovery, linearly calibrated to feelings


def tune_to_feelings(frame: pd.DataFrame, prior: RecoveryModel) -> TuneResult:
    """Fit a personal model to ``feeling`` and compare it, walk-forward, with
    Whoop on the task that matters: predicting how you said you felt."""
    rows = frame.dropna(subset=[*FEATURES, "feeling", "whoop"])
    n = len(rows)
    if n < MIN_FEELING_DAYS:
        raise ValueError(
            f"{n} days have both a feeling score and complete Whoop data; "
            f"need at least {MIN_FEELING_DAYS}"
        )
    X = rows[list(FEATURES)].to_numpy(float)
    y = rows["feeling"].to_numpy(float)
    whoop = rows["whoop"].to_numpy(float)
    steps = range(MIN_TRAIN_DAYS, n)

    # One-step-ahead error of every candidate lam at every step.
    preds = {
        lam: np.array([_predict(_shrunk(X[:t], y[:t], prior, lam), X[t]) for t in steps])
        for lam in LAMBDA_GRID
    }
    errors = {lam: np.abs(p - y[MIN_TRAIN_DAYS:]) for lam, p in preds.items()}

    # Nested choice: at step i use the lam with the lowest error on steps < i,
    # so lam selection never sees the day being scored. With no history yet,
    # fall back to the prior.
    chosen = []
    for i in range(len(steps)):
        lam = math.inf if i == 0 else min(LAMBDA_GRID, key=lambda l: errors[l][:i].mean())
        chosen.append(preds[lam][i])

    calibrated = []
    for t in steps:
        slope, icept = np.polyfit(whoop[:t], y[:t], 1)
        calibrated.append(icept + slope * whoop[t])

    actual = y[MIN_TRAIN_DAYS:]
    final_lam = min(LAMBDA_GRID, key=lambda l: errors[l].mean())
    return TuneResult(
        n_days=n,
        lam=final_lam,
        model=_shrunk(X, y, prior, final_lam),
        personal=_accuracy(np.clip(chosen, 0, 100), actual),
        whoop_weights=_accuracy(np.clip(preds[math.inf], 0, 100), actual),
        whoop_score=_accuracy(np.clip(calibrated, 0, 100), actual),
    )


def group_share(shares: dict[str, float], group: str) -> float:
    return sum(shares[f] for f in GROUPS[group])
