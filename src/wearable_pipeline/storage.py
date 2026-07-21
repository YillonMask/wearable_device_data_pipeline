from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from .models import DailyMetrics, Workout


def write_raw_payload(
    conn: sqlite3.Connection,
    *,
    device: str,
    endpoint: str,
    date: str | None,
    payload: Any,
) -> int:
    cur = conn.execute(
        "INSERT INTO raw_payloads (device, endpoint, date, payload, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            device,
            endpoint,
            date,
            json.dumps(payload),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def upsert_daily_metrics(conn: sqlite3.Connection, metrics: DailyMetrics) -> None:
    payload = metrics.model_dump()
    payload["date"] = metrics.date.isoformat()
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()

    cols = list(payload.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in cols if c not in ("date", "device")
    )
    conn.execute(
        f"INSERT INTO daily_metrics ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(date, device) DO UPDATE SET {updates}",
        [payload[c] for c in cols],
    )
    conn.commit()


def upsert_self_report(
    conn: sqlite3.Connection, *, day: date, readiness: int
) -> None:
    conn.execute(
        "INSERT INTO self_report (date, readiness, logged_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET "
        "  readiness = excluded.readiness, "
        "  logged_at = excluded.logged_at",
        (
            day.isoformat(),
            readiness,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def upsert_workout(conn: sqlite3.Connection, workout: Workout) -> None:
    payload = workout.model_dump()
    payload["start_time"] = workout.start_time.isoformat()
    payload["end_time"] = workout.end_time.isoformat() if workout.end_time else None
    payload["date"] = workout.date.isoformat()
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()

    cols = list(payload.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in cols if c not in ("device", "provider_id")
    )
    conn.execute(
        f"INSERT INTO workouts ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(device, provider_id) DO UPDATE SET {updates}",
        [payload[c] for c in cols],
    )
    conn.commit()


def upsert_manual_readiness(
    conn: sqlite3.Connection,
    *,
    day: date,
    device: str,
    readiness_score: int,
) -> None:
    conn.execute(
        "INSERT INTO manual_metrics (date, device, readiness_score, logged_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(date, device) DO UPDATE SET "
        "  readiness_score = excluded.readiness_score, "
        "  logged_at = excluded.logged_at",
        (
            day.isoformat(),
            device,
            readiness_score,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
