from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from wearable_pipeline import cli, db
from wearable_pipeline.clients.whoop_internal import (
    BASE_URL,
    METRICS_ENDPOINT,
    SIGN_IN_ENDPOINT,
)
from wearable_pipeline.models import Workout
from wearable_pipeline.storage import (
    create_hr_session,
    end_hr_session,
    insert_hr_samples,
    load_hr_session,
    upsert_workout,
)

runner = CliRunner()

SIGN_IN_URL = f"{BASE_URL}{SIGN_IN_ENDPOINT}"
METRICS_URL = f"{BASE_URL}{METRICS_ENDPOINT}/42"

START = datetime(2026, 8, 13, 17, 0, 0, tzinfo=timezone.utc)


def _seed_workout(conn) -> None:
    upsert_workout(
        conn,
        Workout(
            device="whoop",
            provider_id="w-1",
            sport="Swimming",
            start_time=START,
            end_time=datetime(2026, 8, 13, 17, 30, 0, tzinfo=timezone.utc),
            duration_minutes=30,
            avg_hr=132.0,
            max_hr=145.0,
            calories=300,
            date=date(2026, 8, 13),
        ),
    )


def _hr_body(n: int = 5) -> dict:
    base_ms = int(START.timestamp() * 1000)
    return {
        "values": [
            {"time": base_ms + i * 6000, "data": 120 + i} for i in range(n)
        ]
    }


def _mock_whoop(body: dict | None = None) -> None:
    respx.post(SIGN_IN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "user": {"id": 42}}
        )
    )
    respx.get(METRICS_URL).mock(
        return_value=httpx.Response(200, json=body if body is not None else _hr_body())
    )


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WHOOP_USERNAME", "me@example.com")
    monkeypatch.setenv("WHOOP_PASSWORD", "hunter2")
    from wearable_pipeline import config

    monkeypatch.setattr(cli, "load_settings", config.load_settings)
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    return conn


@respx.mock
def test_import_lands_per_sample_series_in_hr_tables(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_workout(conn)
    conn.close()
    _mock_whoop()

    result = runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    session_id = "20260813T170000Z-whoop"
    session, samples = load_hr_session(conn, session_id)
    assert session["label"] == "Swimming"
    assert session["devices"] == ["whoop"]
    assert [s["bpm"] for s in samples] == [120, 121, 122, 123, 124]
    # 6 s spacing preserved as t_offset_ms, anchored at the workout start
    assert [s["t_offset_ms"] for s in samples] == [0, 6000, 12000, 18000, 24000]
    # spacing + the public API's own summary are surfaced for sanity-checking
    assert "spacing=6.0s" in result.output
    assert "API summary avg=132.0 max=145.0" in result.output
    conn.close()


@respx.mock
def test_raw_payload_written_before_samples(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_workout(conn)
    conn.close()
    _mock_whoop()

    runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])

    conn = db.connect(tmp_path / "t.db")
    rows = conn.execute(
        "SELECT device, endpoint FROM raw_payloads WHERE device = 'whoop'"
    ).fetchall()
    assert len(rows) == 1
    assert "heart_rate" in rows[0]["endpoint"]
    conn.close()


@respx.mock
def test_reimport_is_idempotent(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_workout(conn)
    conn.close()
    _mock_whoop()

    runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])
    second = runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])
    assert second.exit_code == 0, second.output

    conn = db.connect(tmp_path / "t.db")
    _, samples = load_hr_session(conn, "20260813T170000Z-whoop")
    assert len(samples) == 5  # not 10
    conn.close()


@respx.mock
def test_buckets_starting_before_the_workout_keep_offsets_non_negative(
    monkeypatch, tmp_path
):
    conn = _setup(monkeypatch, tmp_path)
    _seed_workout(conn)
    conn.close()
    # Whoop aligns buckets to step boundaries, so the first can precede the
    # workout start by up to one step.
    base_ms = int(START.timestamp() * 1000)
    _mock_whoop(
        {
            "values": [
                {"time": base_ms - 4000, "data": 118},
                {"time": base_ms + 2000, "data": 120},
            ]
        }
    )

    result = runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    _, samples = load_hr_session(conn, "20260813T165956Z-whoop")
    assert [s["t_offset_ms"] for s in samples] == [0, 6000]
    conn.close()


@respx.mock
def test_empty_series_still_archives_raw_and_exits_nonzero(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_workout(conn)
    conn.close()
    _mock_whoop({"values": []})

    result = runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])
    assert result.exit_code == 1
    assert "no samples returned" in result.output

    conn = db.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) c FROM raw_payloads").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM hr_samples").fetchone()["c"] == 0
    conn.close()


def test_missing_credentials_exits_2(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("WHOOP_USERNAME", raising=False)
    monkeypatch.delenv("WHOOP_PASSWORD", raising=False)
    from wearable_pipeline import config

    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    result = runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])
    assert result.exit_code == 2
    assert "WHOOP_USERNAME" in result.output


def test_invalid_step_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path).close()
    result = runner.invoke(
        cli.app, ["hr-import-whoop", "--date", "2026-08-13", "--step", "1"]
    )
    assert result.exit_code == 2
    assert "--step must be one of 6, 60, 600" in result.output


def test_no_stored_workouts_points_at_pull(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path).close()
    result = runner.invoke(cli.app, ["hr-import-whoop", "--date", "2026-08-13"])
    assert result.exit_code == 1
    assert "wearable pull --date 2026-08-13" in result.output


def test_requires_exactly_one_of_date_or_session(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path).close()
    neither = runner.invoke(cli.app, ["hr-import-whoop"])
    assert neither.exit_code == 2
    both = runner.invoke(
        cli.app, ["hr-import-whoop", "--date", "2026-08-13", "--session", "S1"]
    )
    assert both.exit_code == 2


# --- --session (merge into an existing BLE capture) --------------------------


def _seed_ble_session(conn) -> None:
    create_hr_session(
        conn,
        session_id="S1",
        label="swim",
        started_at=START.isoformat(),
        devices=["strap", "whoop"],
    )
    rows = []
    for sec in range(0, 30, 2):
        ts = (START + timedelta(seconds=sec)).isoformat()
        rows.append(("S1", "strap", ts, sec * 1000, 140 + (sec % 4)))
    insert_hr_samples(conn, rows)
    end_hr_session(
        conn, session_id="S1", ended_at=(START + timedelta(minutes=30)).isoformat()
    )


@respx.mock
def test_session_merge_lands_as_whoop_api_beside_the_strap(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_ble_session(conn)
    conn.close()
    _mock_whoop()

    result = runner.invoke(cli.app, ["hr-import-whoop", "--session", "S1"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "S1")
    # merged in alongside the live BLE series, not over it
    assert session["devices"] == ["strap", "whoop", "whoop_api"]
    api = [s for s in samples if s["device"] == "whoop_api"]
    strap = [s for s in samples if s["device"] == "strap"]
    assert [s["bpm"] for s in api] == [120, 121, 122, 123, 124]
    assert [s["t_offset_ms"] for s in api] == [0, 6000, 12000, 18000, 24000]
    assert len(strap) == 15  # untouched
    conn.close()


@respx.mock
def test_session_merge_drops_buckets_before_the_session_anchor(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_ble_session(conn)
    conn.close()
    base_ms = int(START.timestamp() * 1000)
    _mock_whoop(
        {
            "values": [
                {"time": base_ms - 6000, "data": 110},  # predates the anchor
                {"time": base_ms + 6000, "data": 121},
            ]
        }
    )

    result = runner.invoke(cli.app, ["hr-import-whoop", "--session", "S1"])
    assert result.exit_code == 0, result.output

    conn = db.connect(tmp_path / "t.db")
    _, samples = load_hr_session(conn, "S1")
    api = [s for s in samples if s["device"] == "whoop_api"]
    assert [s["bpm"] for s in api] == [121]
    assert min(s["t_offset_ms"] for s in api) >= 0
    conn.close()


@respx.mock
def test_session_merge_is_idempotent(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    _seed_ble_session(conn)
    conn.close()
    _mock_whoop()

    runner.invoke(cli.app, ["hr-import-whoop", "--session", "S1"])
    runner.invoke(cli.app, ["hr-import-whoop", "--session", "S1"])

    conn = db.connect(tmp_path / "t.db")
    session, samples = load_hr_session(conn, "S1")
    assert len([s for s in samples if s["device"] == "whoop_api"]) == 5
    assert session["devices"].count("whoop_api") == 1
    conn.close()


def test_session_merge_unknown_session(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path).close()
    result = runner.invoke(cli.app, ["hr-import-whoop", "--session", "nope"])
    assert result.exit_code == 1
    assert "No session" in result.output
