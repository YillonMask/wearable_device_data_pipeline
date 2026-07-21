"""Streamlit visualization for the wearable pipeline.

Launch via ``uv run --extra viz wearable viz`` (which shells out to
``streamlit run`` on this module). Reads ``data/wearable.db`` directly — no
network calls, no auth — so opening the UI is cheap and offline-safe.

Three sections:
  1. Time-series line chart of a chosen metric, one line per device.
  2. Snapshot table of the most-recent day's values across devices.
  3. Cross-device Spearman correlation (reuses ``analysis.run_spearman``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from wearable_pipeline.analysis import run_spearman
from wearable_pipeline.config import load_settings
from wearable_pipeline.models import Workout

# Fields the user can chart. Skip booking columns (date, device, fetched_at).
_METRIC_OPTIONS: tuple[str, ...] = (
    "total_sleep_minutes",
    "sleep_efficiency",
    "sleep_score",
    "readiness_score",
    "hrv_ms",
    "resting_hr",
    "respiratory_rate",
    "rem_minutes",
    "deep_minutes",
    "light_minutes",
    "awake_minutes",
    "sleep_latency_minutes",
    "strain_or_activity_score",
    "steps",
    "active_calories",
    "body_temp_deviation",
    "skin_temp",
)

_DEVICE_ORDER = ("self", "oura", "whoop", "google_health")


@st.cache_data(ttl=60)
def _load_daily_metrics(db_path: str) -> pd.DataFrame:
    """Read daily_metrics, overlay manual_metrics, and append self_report as
    a synthetic device='self' row. Cached for 60s so the daily pull or a fresh
    `wearable log` appears on the next page interaction without manual reload.

    Note: ``self`` readiness is on a 1-100 subjective scale, same range as
    the device readiness scores — but it's still a subjective rating, not a
    physiological measurement, so absolute agreement isn't meaningful. The
    rank-correlation section remains the honest comparison.
    """
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query("SELECT * FROM daily_metrics", conn)
        mm = pd.read_sql_query(
            "SELECT date, device, readiness_score AS _manual_readiness "
            "FROM manual_metrics",
            conn,
        )
        sr = pd.read_sql_query(
            "SELECT date, readiness AS readiness_score FROM self_report", conn
        )

    if not mm.empty:
        df = df.merge(mm, on=["date", "device"], how="outer")
        df["readiness_score"] = df["_manual_readiness"].combine_first(
            df.get("readiness_score")
        )
        df = df.drop(columns=["_manual_readiness"])

    if not sr.empty:
        sr["device"] = "self"
        df = pd.concat([df, sr], ignore_index=True)

    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["date", "device"]).reset_index(drop=True)
    return df


@st.cache_data(ttl=60)
def _load_workouts(db_path: str) -> pd.DataFrame:
    try:
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql_query("SELECT * FROM workouts", conn)
    except (pd.errors.DatabaseError, sqlite3.OperationalError):
        return pd.DataFrame()
    if not df.empty:
        # Whoop start_times carry microseconds (…:33.530000+00:00) while others
        # don't (…:37+00:00); a single inferred format chokes on the mix, so
        # parse as ISO8601 element-by-element. `date` is plain YYYY-MM-DD.
        df["start_time"] = pd.to_datetime(df["start_time"], format="ISO8601")
        df["date"] = pd.to_datetime(df["date"], format="ISO8601")
    return df


def _float_or_none(v: object) -> float | None:
    return None if v is None or pd.isna(v) else float(v)


def _int_or_none_series(v: object) -> int | None:
    return None if v is None or pd.isna(v) else int(v)


def _delta(a: object, b: object) -> object:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return None
    return round(float(a) - float(b), 1)


def _row_to_workout(r: pd.Series) -> Workout:
    end = r.get("end_time")
    end_dt = pd.to_datetime(end).to_pydatetime() if pd.notna(end) else None
    return Workout(
        device=r["device"],
        provider_id=r["provider_id"],
        sport=r.get("sport") if pd.notna(r.get("sport")) else None,
        start_time=r["start_time"].to_pydatetime(),
        end_time=end_dt,
        duration_minutes=_int_or_none_series(r.get("duration_minutes")),
        avg_hr=_float_or_none(r.get("avg_hr")),
        max_hr=_float_or_none(r.get("max_hr")),
        calories=_int_or_none_series(r.get("calories")),
        date=r["date"].date(),
    )


def _format_value(v: object) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _render(db_path: Path) -> None:
    st.set_page_config(
        page_title="Wearable Data Pipeline",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("Wearable Data Pipeline")
    st.caption(
        "Oura · Whoop · Fitbit Air → Google Health · daily metrics, side by side."
    )

    if not db_path.exists():
        st.warning(
            f"Database not found at `{db_path}`. Run "
            "`uv run wearable init` to create it, then "
            "`uv run wearable pull --date yesterday`."
        )
        return

    df = _load_daily_metrics(str(db_path))
    if df.empty:
        st.warning(
            "No rows in `daily_metrics` yet. Run "
            "`uv run wearable pull --date yesterday` first."
        )
        return

    min_date = df["date"].min().date()
    max_date = df["date"].max().date()

    # ---- sidebar filters -------------------------------------------------
    st.sidebar.header("Filters")

    metric = st.sidebar.selectbox("Metric", _METRIC_OPTIONS, index=0)
    date_range = st.sidebar.date_input(
        "Date range",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
    else:
        start = end = (
            date_range
            if not isinstance(date_range, tuple)
            else date_range[0]
        )

    available_devices = sorted(df["device"].unique())
    devices = st.sidebar.multiselect(
        "Devices",
        options=available_devices,
        default=available_devices,
    )

    mask = (
        (df["date"] >= pd.Timestamp(start))
        & (df["date"] <= pd.Timestamp(end))
        & (df["device"].isin(devices))
    )
    view = df.loc[mask].copy()

    if view.empty:
        st.warning("No rows match the current filters.")
        return

    st.sidebar.markdown("---")
    st.sidebar.metric("Days in view", view["date"].nunique())
    st.sidebar.metric("Rows in view", len(view))

    # ---- section 1: time-series chart -----------------------------------
    st.subheader(f"{metric} over time")
    wide = (
        view.pivot(index="date", columns="device", values=metric)
        .reindex(columns=[d for d in _DEVICE_ORDER if d in available_devices])
    )
    st.line_chart(wide, height=360)
    st.caption(
        f"{wide.notna().sum().to_dict()} non-null observations per device."
    )
    if metric == "readiness_score" and "self" in wide.columns:
        st.caption(
            "Note: `self` is your subjective 1-100 log — same range as the "
            "device readiness scores, but a subjective rating rather than a "
            "physiological measurement. The rank-correlation section below "
            "is the honest comparison."
        )

    # ---- section 2: latest-day snapshot ---------------------------------
    st.subheader("Latest-day snapshot")
    latest_date = view["date"].max()
    latest = view[view["date"] == latest_date].set_index("device")
    display_cols = [c for c in _METRIC_OPTIONS if c in latest.columns]
    snapshot = (
        latest[display_cols]
        .reindex(index=[d for d in _DEVICE_ORDER if d in latest.index])
        .T.map(_format_value)
    )
    st.dataframe(snapshot, use_container_width=True)
    st.caption(f"As of {latest_date.date().isoformat()}.")

    # ---- section 3: Spearman correlation --------------------------------
    st.subheader("Cross-device rank correlation (Spearman)")
    st.caption(
        "Each device uses a proprietary scale, so absolute values aren't "
        "comparable. Rank correlation tells you whether two devices agree "
        "about which days were good and which were bad."
    )
    min_n = st.slider(
        "Minimum paired observations to compute rho",
        min_value=2,
        max_value=30,
        value=3,
        help="Pairs with fewer paired observations are still listed, but rho/p are blank.",
    )
    with sqlite3.connect(str(db_path)) as conn:
        rows = run_spearman(conn, since=start, until=end, min_n=min_n)
    corr = pd.DataFrame(
        [
            {
                "metric": r.metric,
                "device_a": r.device_a,
                "device_b": r.device_b,
                "n": r.n,
                "rho": r.rho,
                "p": r.p,
            }
            for r in rows
        ]
    )
    st.dataframe(corr, use_container_width=True, hide_index=True)

    # ---- section 4: workout comparison ----------------------------------
    st.subheader("Workout comparison (Whoop vs Google Health)")
    st.caption(
        "Same workout, measured independently by each device, matched by "
        "overlapping time window. Whoop's calories include BMR, so they read "
        "higher than Fitbit's active-only number — that gap is expected, not a bug. "
        "Google Health doesn't expose per-session max HR, so that column is Whoop-only."
    )
    wdf = _load_workouts(str(db_path))
    if wdf.empty:
        st.info("No workouts pulled yet. Run `uv run wearable pull --workouts`.")
    else:
        wmask = (wdf["date"] >= pd.Timestamp(start)) & (wdf["date"] <= pd.Timestamp(end))
        wview = wdf.loc[wmask]
        whoop_w = [_row_to_workout(r) for _, r in wview[wview["device"] == "whoop"].iterrows()]
        google_w = [_row_to_workout(r) for _, r in wview[wview["device"] == "google_health"].iterrows()]
        from wearable_pipeline.workouts import match_workouts
        res = match_workouts(whoop_w, google_w)
        local_tz = ZoneInfo(load_settings().local_timezone)
        if res.pairs:
            rows = []
            for p in res.pairs:
                rows.append({
                    "start (local)": p.whoop.start_time.astimezone(local_tz).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "sport (whoop)": p.whoop.sport,
                    "avg_hr whoop": p.whoop.avg_hr,
                    "avg_hr google": p.google.avg_hr,
                    "avg_hr Δ": _delta(p.whoop.avg_hr, p.google.avg_hr),
                    "max_hr whoop": p.whoop.max_hr,
                    "cal whoop": p.whoop.calories,
                    "cal google": p.google.calories,
                    "cal Δ": _delta(p.whoop.calories, p.google.calories),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No matched workout pairs in this date range.")
        if res.unmatched_whoop or res.unmatched_google:
            st.caption(
                f"Unmatched — whoop: {len(res.unmatched_whoop)}, "
                f"google_health: {len(res.unmatched_google)} "
                "(a session only one device recorded, or windows that didn't overlap)."
            )

    # ---- raw data expander ----------------------------------------------
    with st.expander("Raw daily_metrics rows"):
        st.dataframe(view, use_container_width=True, hide_index=True)


_render(load_settings().database_path)
