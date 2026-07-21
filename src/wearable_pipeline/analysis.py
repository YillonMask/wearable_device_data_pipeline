"""Cross-device Spearman rank correlation.

Each device's `*_score` fields use a proprietary scale, so comparing absolute
values is meaningless. Rank correlation is the right signal: do two devices
agree about which days were good and which were bad?

We deliberately do NOT compute mean rho across pairs (averaging devices would
itself be ill-defined) and we never impute missing values — dropping null
rows per-pair preserves the comparison's integrity.

``pandas`` and ``scipy`` are imported lazily inside ``run_spearman`` so the
base CLI stays cheap for users who only ingest data.

The synthetic device ``self`` represents the user's subjective 1-100 readiness
from the ``self_report`` table. It only has a value for ``readiness_score``;
other-metric pairs involving ``self`` return n=0.

Manually-entered values from the ``manual_metrics`` table (currently only
``google_health.readiness_score``, since the Fitbit phone app shows it but the
Google Health v4 API does not return it) override the corresponding NULL in
``daily_metrics`` via ``COALESCE``.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date

DEFAULT_METRICS: tuple[str, ...] = (
    "readiness_score",
    "sleep_score",
    "strain_or_activity_score",
    "hrv_ms",
    "resting_hr",
    "total_sleep_minutes",
)

DEFAULT_PAIRS: tuple[tuple[str, str], ...] = (
    ("oura", "whoop"),
    ("oura", "google_health"),
    ("whoop", "google_health"),
    ("self", "oura"),
    ("self", "whoop"),
    ("self", "google_health"),
)


@dataclass(frozen=True)
class SpearmanRow:
    metric: str
    device_a: str
    device_b: str
    n: int
    rho: float | None
    p: float | None


def run_spearman(
    conn: sqlite3.Connection,
    *,
    since: date | None = None,
    until: date | None = None,
    metrics: tuple[str, ...] = DEFAULT_METRICS,
    pairs: tuple[tuple[str, str], ...] = DEFAULT_PAIRS,
    min_n: int = 14,
) -> list[SpearmanRow]:
    import pandas as pd
    from scipy import stats

    where: list[str] = []
    params: list[str] = []
    if since:
        where.append("dm.date >= ?")
        params.append(since.isoformat())
    if until:
        where.append("dm.date <= ?")
        params.append(until.isoformat())
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    metric_select = ", ".join(
        (
            "COALESCE(mm.readiness_score, dm.readiness_score) AS readiness_score"
            if m == "readiness_score"
            else f"dm.{m}"
        )
        for m in metrics
    )
    df = pd.read_sql_query(
        f"SELECT dm.date, dm.device, {metric_select} "
        f"FROM daily_metrics dm "
        f"LEFT JOIN manual_metrics mm "
        f"  ON mm.date = dm.date AND mm.device = dm.device"
        f"{where_clause}",
        conn,
        params=params,
    )

    self_where = ""
    self_params: list[str] = []
    if since or until:
        clauses: list[str] = []
        if since:
            clauses.append("date >= ?")
            self_params.append(since.isoformat())
        if until:
            clauses.append("date <= ?")
            self_params.append(until.isoformat())
        self_where = " WHERE " + " AND ".join(clauses)
    self_df = pd.read_sql_query(
        f"SELECT date, readiness AS readiness_score FROM self_report{self_where}",
        conn,
        params=self_params,
    )
    if not self_df.empty:
        self_df["device"] = "self"
        for m in metrics:
            if m != "readiness_score" and m not in self_df.columns:
                self_df[m] = pd.NA
        self_df = self_df[["date", "device", *metrics]]
        df = pd.concat([df, self_df], ignore_index=True)

    out: list[SpearmanRow] = []
    for metric in metrics:
        wide = df.pivot(index="date", columns="device", values=metric)
        for device_a, device_b in pairs:
            if device_a not in wide.columns or device_b not in wide.columns:
                out.append(
                    SpearmanRow(metric, device_a, device_b, n=0, rho=None, p=None)
                )
                continue
            paired = wide[[device_a, device_b]].dropna()
            n = len(paired)
            if n < min_n:
                out.append(
                    SpearmanRow(metric, device_a, device_b, n=n, rho=None, p=None)
                )
                continue
            rho, p = stats.spearmanr(paired[device_a], paired[device_b])
            out.append(
                SpearmanRow(
                    metric=metric,
                    device_a=device_a,
                    device_b=device_b,
                    n=n,
                    rho=float(rho),
                    p=float(p),
                )
            )
    return out


def format_table(rows: list[SpearmanRow]) -> str:
    if not rows:
        return "(no data)"
    headers = ("metric", "device_a", "device_b", "n", "rho", "p")
    formatted = [
        (
            r.metric,
            r.device_a,
            r.device_b,
            str(r.n),
            f"{r.rho:+.3f}" if r.rho is not None else "—",
            f"{r.p:.4f}" if r.p is not None else "—",
        )
        for r in rows
    ]
    widths = [
        max(len(h), max(len(row[i]) for row in formatted)) for i, h in enumerate(headers)
    ]
    sep = "  "
    out = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append(sep.join("-" * w for w in widths))
    out.extend(sep.join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in formatted)
    return "\n".join(out)


def write_csv(rows: list[SpearmanRow], path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(("metric", "device_a", "device_b", "n", "rho", "p"))
        for r in rows:
            writer.writerow(
                (r.metric, r.device_a, r.device_b, r.n, r.rho, r.p)
            )


def write_json(rows: list[SpearmanRow], path) -> None:
    with open(path, "w") as f:
        json.dump([asdict(r) for r in rows], f, indent=2)
