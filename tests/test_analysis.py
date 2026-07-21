from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from wearable_pipeline import db
from wearable_pipeline.models import DailyMetrics
from wearable_pipeline.storage import upsert_daily_metrics


def test_self_pair_correlates_with_oura_readiness(tmp_path: Path) -> None:
    from wearable_pipeline.analysis import run_spearman
    from wearable_pipeline.storage import upsert_self_report

    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    try:
        start = date(2026, 1, 1)
        # 10 strictly monotonic self values (1..10) matched against 10 strictly
        # monotonic oura values — no ties in either series gives rho = 1.0.
        oura_vals = [60, 65, 70, 75, 80, 82, 85, 88, 92, 99]
        self_vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        for i, (o, s) in enumerate(zip(oura_vals, self_vals)):
            d = start + timedelta(days=i)
            upsert_daily_metrics(
                c, DailyMetrics(date=d, device="oura", readiness_score=o)
            )
            upsert_self_report(c, day=d, readiness=s)

        rows = run_spearman(c, min_n=10)
        by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
        rec = by_pair[("readiness_score", "self", "oura")]
        assert rec.n == 10
        assert rec.rho == pytest.approx(1.0)
    finally:
        c.close()


def test_self_pair_returns_n_zero_for_non_readiness_metrics(tmp_path: Path) -> None:
    from wearable_pipeline.analysis import run_spearman
    from wearable_pipeline.storage import upsert_self_report

    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    try:
        start = date(2026, 1, 1)
        for i in range(15):
            d = start + timedelta(days=i)
            upsert_daily_metrics(
                c,
                DailyMetrics(
                    date=d, device="oura", sleep_score=80, hrv_ms=45.0
                ),
            )
            upsert_self_report(c, day=d, readiness=7)

        rows = run_spearman(c, min_n=14)
        by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
        rec = by_pair[("sleep_score", "self", "oura")]
        assert rec.n == 0
        assert rec.rho is None
    finally:
        c.close()


def test_manual_readiness_fills_google_health_gap(tmp_path: Path) -> None:
    from wearable_pipeline.analysis import run_spearman
    from wearable_pipeline.storage import upsert_manual_readiness

    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    try:
        start = date(2026, 1, 1)
        oura_vals = [60, 65, 70, 72, 75, 80, 81, 82, 85, 88, 90, 92, 94, 95, 99]
        fitbit_manual_vals = [40, 45, 50, 55, 60, 65, 70, 72, 75, 78, 82, 85, 87, 88, 91]
        for i, (o, f) in enumerate(zip(oura_vals, fitbit_manual_vals)):
            d = start + timedelta(days=i)
            upsert_daily_metrics(
                c, DailyMetrics(date=d, device="oura", readiness_score=o)
            )
            upsert_daily_metrics(
                c,
                DailyMetrics(date=d, device="google_health", readiness_score=None),
            )
            upsert_manual_readiness(
                c, day=d, device="google_health", readiness_score=f
            )

        rows = run_spearman(c, min_n=14)
        by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
        rec = by_pair[("readiness_score", "oura", "google_health")]
        assert rec.n == 15
        assert rec.rho == pytest.approx(1.0)
    finally:
        c.close()


def _seed(conn, metric: str, oura_values: list[float], whoop_values: list[float]) -> None:
    """Write paired (oura, whoop) values for `metric` across consecutive days."""
    start = date(2026, 1, 1)
    for i, (o, w) in enumerate(zip(oura_values, whoop_values)):
        d = start + timedelta(days=i)
        upsert_daily_metrics(
            conn, DailyMetrics(date=d, device="oura", **{metric: o})
        )
        upsert_daily_metrics(
            conn, DailyMetrics(date=d, device="whoop", **{metric: w})
        )


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


def test_perfectly_correlated_pairs_give_rho_one(conn) -> None:
    from wearable_pipeline.analysis import run_spearman

    _seed(
        conn,
        "readiness_score",
        oura_values=[60, 65, 70, 72, 75, 80, 81, 82, 85, 88, 90, 92, 94, 95, 99],
        whoop_values=[40, 45, 50, 55, 60, 65, 70, 72, 75, 78, 82, 85, 87, 88, 91],
    )
    rows = run_spearman(conn, min_n=14)
    by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
    rec = by_pair[("readiness_score", "oura", "whoop")]
    assert rec.n == 15
    assert rec.rho == pytest.approx(1.0)
    assert rec.p == pytest.approx(0.0, abs=1e-7)


def test_perfectly_anticorrelated_pairs_give_rho_negative_one(conn) -> None:
    from wearable_pipeline.analysis import run_spearman

    n = 15
    _seed(
        conn,
        "hrv_ms",
        oura_values=[float(i) for i in range(n)],
        whoop_values=[float(n - i) for i in range(n)],
    )
    rows = run_spearman(conn, min_n=14)
    by_pair = {(r.metric, r.device_a, r.device_b): r for r in rows}
    rec = by_pair[("hrv_ms", "oura", "whoop")]
    assert rec.n == n
    assert rec.rho == pytest.approx(-1.0)


def test_below_min_n_returns_no_rho(conn) -> None:
    from wearable_pipeline.analysis import run_spearman

    _seed(
        conn,
        "readiness_score",
        oura_values=[50, 60, 70, 80, 90],
        whoop_values=[40, 50, 60, 70, 80],
    )
    rows = run_spearman(conn, min_n=14)
    rec = next(
        r for r in rows
        if r.metric == "readiness_score" and r.device_a == "oura" and r.device_b == "whoop"
    )
    assert rec.n == 5
    assert rec.rho is None
    assert rec.p is None


def test_missing_device_returns_zero_n(conn) -> None:
    """Only Oura data → pairs involving whoop/google_health show n=0."""
    from wearable_pipeline.analysis import run_spearman

    start = date(2026, 1, 1)
    for i in range(20):
        upsert_daily_metrics(
            conn,
            DailyMetrics(date=start + timedelta(days=i), device="oura", readiness_score=70),
        )
    rows = run_spearman(conn, min_n=14)
    for r in rows:
        assert r.n == 0
        assert r.rho is None


def test_csv_and_json_writers(conn, tmp_path: Path) -> None:
    from wearable_pipeline.analysis import run_spearman, write_csv, write_json

    _seed(
        conn,
        "sleep_score",
        oura_values=[50 + i for i in range(15)],
        whoop_values=[45 + i for i in range(15)],
    )
    rows = run_spearman(conn, min_n=14)

    csv_path = tmp_path / "out.csv"
    json_path = tmp_path / "out.json"
    write_csv(rows, csv_path)
    write_json(rows, json_path)

    csv_rows = list(csv.DictReader(csv_path.open()))
    assert csv_rows[0].keys() == {"metric", "device_a", "device_b", "n", "rho", "p"}
    assert any(r["metric"] == "sleep_score" and r["device_a"] == "oura" for r in csv_rows)

    data = json.loads(json_path.read_text())
    assert isinstance(data, list)
    assert {"metric", "device_a", "device_b", "n", "rho", "p"} <= set(data[0].keys())


def test_since_filter_limits_rows_used(conn) -> None:
    """Rows outside [since, until] are excluded from the correlation."""
    from wearable_pipeline.analysis import run_spearman

    # Seed 20 paired days starting 2026-01-01.
    _seed(
        conn,
        "readiness_score",
        oura_values=[40 + i for i in range(20)],
        whoop_values=[20 + i for i in range(20)],
    )

    # Restrict to the last 5 days — below min_n=14, so rho should be None.
    rows = run_spearman(conn, since=date(2026, 1, 16), min_n=14)
    rec = next(
        r for r in rows
        if r.metric == "readiness_score" and r.device_a == "oura" and r.device_b == "whoop"
    )
    assert rec.n == 5
    assert rec.rho is None
