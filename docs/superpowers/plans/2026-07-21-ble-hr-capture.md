# Live BLE Heart-Rate Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture live heart rate from Whoop 5.0 and Fitbit Air simultaneously over Bluetooth LE on a MacBook, store it in the existing SQLite DB, and compare the two devices second-by-second.

**Architecture:** A new `capture/` subpackage holds the BLE concern (kept out of the HTTP `clients/` protocol). A single `bleak` central connects both peripherals; one shared clock timestamps both streams. Three CLI commands — `hr-scan`, `hr-capture`, `hr-compare` — wire scanning, capture, and analysis. Two new tables (`hr_sessions`, `hr_samples`) via migration `0005`.

**Tech Stack:** Python ≥3.11, `bleak` (BLE), `typer` (CLI), `sqlite3`, `pandas`/`scipy`/`matplotlib` (compare), `pytest` (TDD).

## Global Constraints

- Python `>=3.11`; project uses `from __future__ import annotations` at the top of every module.
- Device vocabulary is exactly `"whoop"` and `"google_health"` (Fitbit Air == `google_health`); `hr-compare` renders `google_health` as `"Fitbit Air"`.
- All timestamps stored as ISO-8601 UTC **TEXT**; time offsets as **INTEGER** milliseconds. No SQLite-specific types; no reliance on ROWID.
- Add a **new** migration file only (`0005_hr_capture.sql`); never edit prior migrations.
- Standard BLE Heart Rate Profile UUIDs: service `0000180d-0000-1000-8000-00805f9b34fb`, measurement characteristic `00002a37-0000-1000-8000-00805f9b34fb`.
- New optional-dependency extra `ble = ["bleak>=0.22"]`; add `matplotlib>=3.8` to the existing `analysis` extra. `hr-capture` requires `ble`; `hr-compare` requires `analysis`. Each command must error with the exact `uv sync --extra …` hint when its dependency is missing.
- **v1 captures BPM only** — RR-intervals / HRV / energy-expended fields in the characteristic are parsed past and discarded. No reserved columns.
- Connect by **fixed BLE address** from `.env` (`WHOOP_BLE_ADDRESS`, `FITBIT_BLE_ADDRESS`).
- macOS requires the terminal app to have **Bluetooth** permission (System Settings → Privacy & Security → Bluetooth).
- storage functions follow the existing pattern: take `conn` first, mutate, `conn.commit()`.
- TDD throughout; commit after each task. No HTTP here, so `respx` is not used.

---

### Task 1: Dependencies & config wiring

**Files:**
- Modify: `pyproject.toml` (add `ble` extra; add `matplotlib` to `analysis`)
- Modify: `.env.example` (add BLE address placeholders)
- Modify: `src/wearable_pipeline/config.py` (add two settings)
- Test: `tests/test_config_ble.py`

**Interfaces:**
- Produces: `Settings.whoop_ble_address: str | None`, `Settings.fitbit_ble_address: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_ble.py
from __future__ import annotations

import importlib


def test_ble_addresses_loaded_from_env(monkeypatch):
    monkeypatch.setenv("WHOOP_BLE_ADDRESS", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("FITBIT_BLE_ADDRESS", "11:22:33:44:55:66")
    from wearable_pipeline import config
    importlib.reload(config)
    s = config.load_settings()
    assert s.whoop_ble_address == "AA:BB:CC:DD:EE:FF"
    assert s.fitbit_ble_address == "11:22:33:44:55:66"


def test_ble_addresses_default_none(monkeypatch):
    monkeypatch.delenv("WHOOP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("FITBIT_BLE_ADDRESS", raising=False)
    from wearable_pipeline import config
    importlib.reload(config)
    s = config.load_settings()
    assert s.whoop_ble_address is None
    assert s.fitbit_ble_address is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_ble.py -v`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'whoop_ble_address'`).

- [ ] **Step 3: Add the two fields to `Settings` and `load_settings`**

In `src/wearable_pipeline/config.py`, add to the `Settings` dataclass (after `google_refresh_token`):

```python
    whoop_ble_address: str | None
    fitbit_ble_address: str | None
```

And to the `load_settings()` return (after `google_refresh_token=...`):

```python
        whoop_ble_address=_opt("WHOOP_BLE_ADDRESS"),
        fitbit_ble_address=_opt("FITBIT_BLE_ADDRESS"),
```

- [ ] **Step 4: Update `pyproject.toml`**

Add to `[project.optional-dependencies]`:

```toml
ble = [
    "bleak>=0.22",
]
```

And change the existing `analysis` extra to include matplotlib:

```toml
analysis = [
    "pandas>=2.2",
    "scipy>=1.13",
    "matplotlib>=3.8",
]
```

- [ ] **Step 5: Update `.env.example`**

Append:

```bash
# BLE heart-rate capture (run `uv run wearable hr-scan` to discover addresses)
WHOOP_BLE_ADDRESS=
FITBIT_BLE_ADDRESS=
```

- [ ] **Step 6: Sync and run tests**

Run: `uv sync --extra dev --extra ble && uv run pytest tests/test_config_ble.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .env.example src/wearable_pipeline/config.py tests/test_config_ble.py
git commit -m "feat(ble): add bleak extra, matplotlib to analysis, BLE address settings"
```

---

### Task 2: HR Measurement parser

**Files:**
- Create: `src/wearable_pipeline/capture/__init__.py` (empty)
- Create: `src/wearable_pipeline/capture/ble_hr.py`
- Test: `tests/test_ble_parse.py`

**Interfaces:**
- Produces: `parse_hr_measurement(data: bytes) -> int` — returns BPM, ignoring any trailing RR/energy bytes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ble_parse.py
from __future__ import annotations

import pytest

from wearable_pipeline.capture.ble_hr import parse_hr_measurement


def test_uint8_format():
    # flags=0x00 -> 8-bit HR in byte 1
    assert parse_hr_measurement(bytes([0x00, 72])) == 72


def test_uint16_format():
    # flags=0x01 -> 16-bit little-endian HR in bytes 1..2 (300 = 0x012C)
    assert parse_hr_measurement(bytes([0x01, 0x2C, 0x01])) == 300


def test_ignores_trailing_rr_and_energy_bytes():
    # flags=0x00, HR=65, then two trailing RR-interval bytes -> still 65
    assert parse_hr_measurement(bytes([0x00, 65, 0x00, 0x02])) == 65


def test_empty_payload_raises():
    with pytest.raises(ValueError):
        parse_hr_measurement(b"")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ble_parse.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'wearable_pipeline.capture'`).

- [ ] **Step 3: Create the package and parser**

Create empty `src/wearable_pipeline/capture/__init__.py`.

Create `src/wearable_pipeline/capture/ble_hr.py`:

```python
from __future__ import annotations

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


def parse_hr_measurement(data: bytes) -> int:
    """Decode BPM from a BLE Heart Rate Measurement (0x2A37) payload.

    Byte 0 is a flags field; bit 0 selects the HR value format:
    0 -> uint8 in byte 1, 1 -> uint16 little-endian in bytes 1..2.
    Any trailing fields (energy expended, RR-intervals) are ignored.
    """
    if len(data) < 2:
        raise ValueError(f"HR measurement payload too short: {data!r}")
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            raise ValueError(f"16-bit HR flagged but payload too short: {data!r}")
        return int.from_bytes(data[1:3], "little")
    return data[1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ble_parse.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/wearable_pipeline/capture/__init__.py src/wearable_pipeline/capture/ble_hr.py tests/test_ble_parse.py
git commit -m "feat(ble): add HR Measurement characteristic parser"
```

---

### Task 3: BLE scan + notification handler factory

**Files:**
- Modify: `src/wearable_pipeline/capture/ble_hr.py`
- Test: `tests/test_ble_handler.py`

**Interfaces:**
- Consumes: `parse_hr_measurement`, `HR_SERVICE_UUID`, `HR_MEASUREMENT_UUID` (Task 2).
- Produces:
  - `Sample = tuple[str, str, int, int]` — `(device, ts_utc_iso, t_offset_ms, bpm)`.
  - `make_notification_handler(device, session_start, on_sample) -> Callable[[Any, bytearray], None]` — decodes a notification and calls `on_sample(Sample)`. `session_start` is a `datetime` (UTC).
  - `async scan_hr_peripherals(timeout: float = 8.0) -> list[tuple[str, str]]` — `(name, address)` of nearby HR-service peripherals.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ble_handler.py
from __future__ import annotations

from datetime import datetime, timezone

from wearable_pipeline.capture.ble_hr import make_notification_handler


def test_handler_emits_sample_with_offset():
    start = datetime(2026, 7, 21, 18, 30, 0, tzinfo=timezone.utc)
    got = []
    handler = make_notification_handler("whoop", start, got.append)

    # Call twice with uint8 payloads; monkeypatch the clock via injected now()
    handler(None, bytearray([0x00, 130]))
    handler(None, bytearray([0x00, 131]))

    assert [s[0] for s in got] == ["whoop", "whoop"]
    assert [s[3] for s in got] == [130, 131]
    # ts is ISO UTC, offset is a non-negative int
    assert got[0][1].endswith("+00:00")
    assert isinstance(got[0][2], int) and got[0][2] >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ble_handler.py -v`
Expected: FAIL (`ImportError: cannot import name 'make_notification_handler'`).

- [ ] **Step 3: Implement handler factory and scan**

Append to `src/wearable_pipeline/capture/ble_hr.py`:

```python
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

Sample = tuple[str, str, int, int]  # (device, ts_utc_iso, t_offset_ms, bpm)


def make_notification_handler(
    device: str,
    session_start: datetime,
    on_sample: Callable[[Sample], None],
) -> Callable[[Any, bytearray], None]:
    """Build a bleak notification callback that decodes and dispatches a Sample."""

    def handler(_char: Any, data: bytearray) -> None:
        now = datetime.now(timezone.utc)
        bpm = parse_hr_measurement(bytes(data))
        offset_ms = int((now - session_start).total_seconds() * 1000)
        on_sample((device, now.isoformat(), max(offset_ms, 0), bpm))

    return handler


async def scan_hr_peripherals(timeout: float = 8.0) -> list[tuple[str, str]]:
    """Discover nearby BLE peripherals advertising the Heart Rate service."""
    from bleak import BleakScanner

    devices = await BleakScanner.discover(
        timeout=timeout, service_uuids=[HR_SERVICE_UUID]
    )
    return [(d.name or "(unknown)", d.address) for d in devices]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ble_handler.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/wearable_pipeline/capture/ble_hr.py tests/test_ble_handler.py
git commit -m "feat(ble): add scan + notification handler factory"
```

---

### Task 4: `hr-scan` CLI command

**Files:**
- Modify: `src/wearable_pipeline/cli.py`
- Test: `tests/test_cli_hr_scan.py`

**Interfaces:**
- Consumes: `scan_hr_peripherals` (Task 3).
- Produces: `wearable hr-scan` — prints a table of discovered `(name, address)` peripherals and a hint to paste addresses into `.env`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_hr_scan.py
from __future__ import annotations

from typer.testing import CliRunner

from wearable_pipeline import cli

runner = CliRunner()


def test_hr_scan_lists_devices(monkeypatch):
    async def fake_scan(timeout: float = 8.0):
        return [("WHOOP", "AA:BB:CC:DD:EE:FF"), ("Fitbit Air", "11:22:33:44:55:66")]

    monkeypatch.setattr(cli, "scan_hr_peripherals", fake_scan)
    result = runner.invoke(cli.app, ["hr-scan"])
    assert result.exit_code == 0
    assert "AA:BB:CC:DD:EE:FF" in result.stdout
    assert "Fitbit Air" in result.stdout


def test_hr_scan_missing_bleak(monkeypatch):
    def boom(timeout: float = 8.0):
        raise ImportError("no bleak")

    monkeypatch.setattr(cli, "scan_hr_peripherals", boom)
    result = runner.invoke(cli.app, ["hr-scan"])
    assert result.exit_code == 2
    assert "uv sync --extra ble" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_hr_scan.py -v`
Expected: FAIL (`no such command 'hr-scan'`, exit code 2 / usage error).

- [ ] **Step 3: Implement the command**

In `src/wearable_pipeline/cli.py`, add near the other imports:

```python
import asyncio

from .capture.ble_hr import scan_hr_peripherals
```

Add the command (place after the `auth` command):

```python
@app.command("hr-scan")
def hr_scan(
    timeout: float = typer.Option(8.0, "--timeout", help="Scan seconds."),
) -> None:
    """Scan for nearby BLE heart-rate peripherals; paste addresses into .env."""
    try:
        found = asyncio.run(scan_hr_peripherals(timeout))
    except ImportError:
        typer.echo(
            "bleak not installed. Install with: `uv sync --extra ble`", err=True
        )
        raise typer.Exit(code=2)

    if not found:
        typer.echo("No HR peripherals found. Enable HR broadcast on each device.")
        raise typer.Exit(code=0)

    typer.echo(f"{'NAME':<24s} ADDRESS")
    for name, address in found:
        typer.echo(f"{name:<24s} {address}")
    typer.echo(
        "\nSet WHOOP_BLE_ADDRESS / FITBIT_BLE_ADDRESS in .env to the matching "
        "addresses above."
    )
```

> Note: the command references the module-level name `scan_hr_peripherals`, which the tests monkeypatch on `cli`. `typer`'s `CliRunner` captures stderr into `.stdout` by default, so the assertions on `result.stdout` hold.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_hr_scan.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/wearable_pipeline/cli.py tests/test_cli_hr_scan.py
git commit -m "feat(ble): add hr-scan CLI command"
```

---

### Task 5: 🔬 MANUAL FEASIBILITY SPIKE (gate)

**No code / no test — this is a hardware checkpoint. Do it before Tasks 6–10.**

**Purpose:** confirm the Mac can actually receive HR notifications from *both* devices — especially Fitbit Air's proximity-selection flow — before investing in storage, capture, and compare.

- [ ] **Step 1: Grant Bluetooth permission**
  System Settings → Privacy & Security → Bluetooth → enable your terminal app (Terminal / iTerm). Restart the terminal.

- [ ] **Step 2: Enable broadcast on each device**
  - Whoop: app → Device Settings → toggle **HR Broadcast** ON.
  - Fitbit Air: Google Health app → Connections → **Share Heart Rate**, bring device near the Mac.

- [ ] **Step 3: Scan**
  Run: `uv run wearable hr-scan`
  Expected: both a WHOOP-like entry and a Fitbit Air entry appear with addresses. Record both addresses into `.env` (`WHOOP_BLE_ADDRESS`, `FITBIT_BLE_ADDRESS`).

- [ ] **Step 4: Minimal connect-and-read (throwaway probe)**
  Run this one-off (not committed) to confirm live notifications from each address:

```bash
uv run --extra ble python - "$WHOOP_BLE_ADDRESS" "$FITBIT_BLE_ADDRESS" <<'PY'
import asyncio, sys
from bleak import BleakClient
from wearable_pipeline.capture.ble_hr import HR_MEASUREMENT_UUID, parse_hr_measurement

async def read_one(addr):
    async with BleakClient(addr) as c:
        seen = 0
        def cb(_h, data):
            nonlocal seen
            seen += 1
            print(addr, parse_hr_measurement(bytes(data)))
        await c.start_notify(HR_MEASUREMENT_UUID, cb)
        await asyncio.sleep(8)
        await c.stop_notify(HR_MEASUREMENT_UUID)
        return seen

for a in sys.argv[1:]:
    n = asyncio.run(read_one(a))
    print(f"{a}: {n} notifications")
PY
```
  Expected: nonzero BPM printed for **each** address.

- [ ] **Step 5: Decision gate**
  - ✅ Both stream → proceed to Task 6.
  - ❌ Fitbit Air will not connect to the laptop central → **STOP and report to the owner.** Do not build the rest. Capture the exact bleak error for the report.

---

### Task 6: Migration + HR storage functions

**Files:**
- Create: `migrations/0005_hr_capture.sql`
- Modify: `src/wearable_pipeline/storage.py`
- Test: `tests/test_hr_storage.py`

**Interfaces:**
- Produces (in `storage.py`):
  - `create_hr_session(conn, *, session_id: str, label: str | None, started_at: str, devices: list[str]) -> None`
  - `insert_hr_samples(conn, samples: list[tuple[str, str, str, int, int]]) -> None` — each tuple is `(session_id, device, ts_utc, t_offset_ms, bpm)`.
  - `end_hr_session(conn, *, session_id: str, ended_at: str) -> None`
  - `load_hr_session(conn, session_id: str) -> tuple[dict, list[dict]]` — `(session_dict, sample_dicts)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hr_storage.py
from __future__ import annotations

from wearable_pipeline.storage import (
    create_hr_session,
    end_hr_session,
    insert_hr_samples,
    load_hr_session,
)


def test_session_roundtrip(migrated_db):
    conn = migrated_db
    create_hr_session(
        conn,
        session_id="20260721T183005Z",
        label="bike",
        started_at="2026-07-21T18:30:05+00:00",
        devices=["whoop", "google_health"],
    )
    insert_hr_samples(
        conn,
        [
            ("20260721T183005Z", "whoop", "2026-07-21T18:30:06+00:00", 1000, 120),
            ("20260721T183005Z", "google_health", "2026-07-21T18:30:06+00:00", 1000, 118),
        ],
    )
    end_hr_session(conn, session_id="20260721T183005Z", ended_at="2026-07-21T19:00:00+00:00")

    session, samples = load_hr_session(conn, "20260721T183005Z")
    assert session["label"] == "bike"
    assert session["ended_at"] == "2026-07-21T19:00:00+00:00"
    assert session["devices"] == ["whoop", "google_health"]
    assert len(samples) == 2
    assert {s["device"] for s in samples} == {"whoop", "google_health"}
    assert samples[0]["bpm"] in (118, 120)


def test_load_missing_session_raises(migrated_db):
    import pytest

    with pytest.raises(KeyError):
        load_hr_session(migrated_db, "nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hr_storage.py -v`
Expected: FAIL (`ImportError: cannot import name 'create_hr_session'`).

- [ ] **Step 3: Create the migration**

Create `migrations/0005_hr_capture.sql`:

```sql
CREATE TABLE hr_sessions (
    id          TEXT PRIMARY KEY,   -- compact ISO-8601 UTC start, e.g. 20260721T183005Z
    label       TEXT,               -- 'bike' | 'treadmill' | ...
    started_at  TEXT NOT NULL,      -- ISO-8601 UTC (extended)
    ended_at    TEXT,               -- ISO-8601 UTC; NULL until stopped
    devices     TEXT                -- JSON array, e.g. ["whoop","google_health"]
);

CREATE TABLE hr_samples (
    session_id  TEXT NOT NULL,      -- FK -> hr_sessions.id
    device      TEXT NOT NULL,      -- 'whoop' | 'google_health'
    ts_utc      TEXT NOT NULL,      -- receipt time, ISO-8601 UTC
    t_offset_ms INTEGER NOT NULL,   -- ms since session start
    bpm         INTEGER NOT NULL,
    PRIMARY KEY (session_id, device, ts_utc)
);

CREATE INDEX idx_hr_samples_session ON hr_samples (session_id, device);
```

- [ ] **Step 4: Add storage functions**

Append to `src/wearable_pipeline/storage.py`:

```python
def create_hr_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    label: str | None,
    started_at: str,
    devices: list[str],
) -> None:
    conn.execute(
        "INSERT INTO hr_sessions (id, label, started_at, ended_at, devices) "
        "VALUES (?, ?, ?, NULL, ?)",
        (session_id, label, started_at, json.dumps(devices)),
    )
    conn.commit()


def insert_hr_samples(
    conn: sqlite3.Connection,
    samples: list[tuple[str, str, str, int, int]],
) -> None:
    """Each tuple: (session_id, device, ts_utc, t_offset_ms, bpm)."""
    if not samples:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO hr_samples "
        "(session_id, device, ts_utc, t_offset_ms, bpm) VALUES (?, ?, ?, ?, ?)",
        samples,
    )
    conn.commit()


def end_hr_session(
    conn: sqlite3.Connection, *, session_id: str, ended_at: str
) -> None:
    conn.execute(
        "UPDATE hr_sessions SET ended_at = ? WHERE id = ?",
        (ended_at, session_id),
    )
    conn.commit()


def load_hr_session(
    conn: sqlite3.Connection, session_id: str
) -> tuple[dict, list[dict]]:
    row = conn.execute(
        "SELECT id, label, started_at, ended_at, devices FROM hr_sessions "
        "WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no hr_session with id {session_id!r}")
    session = dict(row)
    session["devices"] = json.loads(session["devices"]) if session["devices"] else []
    samples = [
        dict(r)
        for r in conn.execute(
            "SELECT device, ts_utc, t_offset_ms, bpm FROM hr_samples "
            "WHERE session_id = ? ORDER BY t_offset_ms",
            (session_id,),
        )
    ]
    return session, samples
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_hr_storage.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add migrations/0005_hr_capture.sql src/wearable_pipeline/storage.py tests/test_hr_storage.py
git commit -m "feat(ble): add hr_sessions/hr_samples schema + storage"
```

---

### Task 7: Capture session orchestration (`capture_session`)

**Files:**
- Modify: `src/wearable_pipeline/capture/ble_hr.py`
- Test: `tests/test_capture_session.py`

**Interfaces:**
- Consumes: `make_notification_handler`, `HR_MEASUREMENT_UUID` (Tasks 2–3).
- Produces: `async capture_session(addresses: dict[str, str], session_start: datetime, on_sample: Callable[[Sample], None], stop_event: asyncio.Event, *, client_factory=None, reconnect_attempts: int = 3) -> dict[str, int]` — connects each `device -> address`, subscribes, forwards samples via `on_sample`, runs until `stop_event` is set, returns `{device: samples_received}`. `client_factory(address)` yields an async-context-manager BLE client (defaults to `bleak.BleakClient`); injectable for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capture_session.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from wearable_pipeline.capture.ble_hr import capture_session


class FakeClient:
    """Minimal async-context BLE client that emits a few HR notifications."""

    def __init__(self, address: str):
        self.address = address

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def start_notify(self, uuid, handler):
        # emit three uint8 notifications
        for bpm in (100, 101, 102):
            handler(None, bytearray([0x00, bpm]))
            await asyncio.sleep(0)

    async def stop_notify(self, uuid):
        return None


def test_capture_collects_from_both_devices():
    start = datetime(2026, 7, 21, 18, 30, 0, tzinfo=timezone.utc)
    got = []
    stop = asyncio.Event()

    async def scenario():
        # stop shortly after notifications are delivered
        async def stopper():
            await asyncio.sleep(0.05)
            stop.set()

        counts, _ = await asyncio.gather(
            capture_session(
                {"whoop": "A", "google_health": "B"},
                start,
                got.append,
                stop,
                client_factory=FakeClient,
            ),
            stopper(),
        )
        return counts

    counts = asyncio.run(scenario())
    assert counts["whoop"] == 3
    assert counts["google_health"] == 3
    assert {s[0] for s in got} == {"whoop", "google_health"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capture_session.py -v`
Expected: FAIL (`ImportError: cannot import name 'capture_session'`).

- [ ] **Step 3: Implement `capture_session`**

Append to `src/wearable_pipeline/capture/ble_hr.py`:

```python
import asyncio
import logging

logger = logging.getLogger("wearable_pipeline.capture")


async def capture_session(
    addresses: dict[str, str],
    session_start: datetime,
    on_sample: Callable[[Sample], None],
    stop_event: asyncio.Event,
    *,
    client_factory: Callable[[str], Any] | None = None,
    reconnect_attempts: int = 3,
) -> dict[str, int]:
    """Connect each device, forward samples via on_sample until stop_event set.

    Returns a per-device count of samples received. A device that keeps failing
    to connect is logged and skipped; the others continue (partial tolerance).
    """
    if client_factory is None:
        from bleak import BleakClient

        client_factory = BleakClient

    counts: dict[str, int] = {d: 0 for d in addresses}

    def counting_sink(device: str):
        base = make_notification_handler(device, session_start, on_sample)

        def wrapped(char: Any, data: bytearray) -> None:
            counts[device] += 1
            base(char, data)

        return wrapped

    async def run_device(device: str, address: str) -> None:
        for attempt in range(1, reconnect_attempts + 1):
            try:
                async with client_factory(address) as client:
                    await client.start_notify(
                        HR_MEASUREMENT_UUID, counting_sink(device)
                    )
                    await stop_event.wait()
                    await client.stop_notify(HR_MEASUREMENT_UUID)
                return
            except Exception as exc:  # noqa: BLE001 - log and retry/skip
                logger.warning(
                    "device %s connect/notify failed (attempt %d/%d): %s",
                    device, attempt, reconnect_attempts, exc,
                )
                if stop_event.is_set():
                    return
                await asyncio.sleep(1.0)
        logger.error("device %s gave up after %d attempts", device, reconnect_attempts)

    await asyncio.gather(*(run_device(d, a) for d, a in addresses.items()))
    return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_capture_session.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/wearable_pipeline/capture/ble_hr.py tests/test_capture_session.py
git commit -m "feat(ble): add capture_session orchestration with reconnect"
```

---

### Task 8: `hr-capture` CLI command

**Files:**
- Modify: `src/wearable_pipeline/cli.py`
- Test: `tests/test_cli_hr_capture.py`

**Interfaces:**
- Consumes: `capture_session` (Task 7); `create_hr_session`, `insert_hr_samples`, `end_hr_session` (Task 6); `load_settings`, `db` (existing).
- Produces: `wearable hr-capture [--label L] [--minutes N]` — creates a session, streams samples to the DB with a live readout, stops on Ctrl-C or after `--minutes`, prints a summary. Reads addresses from settings; errors if either is missing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_hr_capture.py
from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from wearable_pipeline import cli

runner = CliRunner()


def test_hr_capture_missing_addresses(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("WHOOP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("FITBIT_BLE_ADDRESS", raising=False)
    import importlib
    from wearable_pipeline import config
    importlib.reload(config)
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    result = runner.invoke(cli.app, ["hr-capture", "--minutes", "0"])
    assert result.exit_code == 2
    assert "hr-scan" in result.stdout


def test_hr_capture_records_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WHOOP_BLE_ADDRESS", "A")
    monkeypatch.setenv("FITBIT_BLE_ADDRESS", "B")
    import importlib
    from wearable_pipeline import config
    importlib.reload(config)
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    async def fake_capture(addresses, start, on_sample, stop_event, **kw):
        on_sample(("whoop", start.isoformat(), 1000, 120))
        on_sample(("google_health", start.isoformat(), 1000, 118))
        return {"whoop": 1, "google_health": 1}

    monkeypatch.setattr(cli, "capture_session", fake_capture)

    result = runner.invoke(cli.app, ["hr-capture", "--label", "bike", "--minutes", "0"])
    assert result.exit_code == 0, result.stdout
    assert "bike" in result.stdout

    # verify rows landed
    from wearable_pipeline import db
    conn = db.connect(tmp_path / "t.db")
    n = conn.execute("SELECT COUNT(*) FROM hr_samples").fetchone()[0]
    assert n == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_hr_capture.py -v`
Expected: FAIL (`no such command 'hr-capture'`).

- [ ] **Step 3: Implement the command**

In `src/wearable_pipeline/cli.py` add imports:

```python
from datetime import datetime, timezone

from .capture.ble_hr import capture_session
from .storage import create_hr_session, end_hr_session, insert_hr_samples
```

Add the command:

```python
@app.command("hr-capture")
def hr_capture(
    label: str | None = typer.Option(None, "--label", help="Session label, e.g. 'bike'."),
    minutes: float | None = typer.Option(
        None, "--minutes", help="Auto-stop after N minutes (else Ctrl-C)."
    ),
) -> None:
    """Capture live HR from Whoop + Fitbit Air over BLE into the DB."""
    settings = load_settings()
    if not settings.whoop_ble_address or not settings.fitbit_ble_address:
        typer.echo(
            "WHOOP_BLE_ADDRESS / FITBIT_BLE_ADDRESS missing from .env. "
            "Run `wearable hr-scan` to discover them.",
            err=True,
        )
        raise typer.Exit(code=2)

    configure_logging()
    conn = db.connect(settings.database_path)
    db.migrate(conn)

    start = datetime.now(timezone.utc)
    session_id = start.strftime("%Y%m%dT%H%M%SZ")
    devices = ["whoop", "google_health"]
    create_hr_session(
        conn,
        session_id=session_id,
        label=label,
        started_at=start.isoformat(),
        devices=devices,
    )

    addresses = {
        "whoop": settings.whoop_ble_address,
        "google_health": settings.fitbit_ble_address,
    }
    latest: dict[str, int] = {}
    buffer: list[tuple[str, str, str, int, int]] = []

    def on_sample(sample) -> None:
        device, ts_utc, offset_ms, bpm = sample
        latest[device] = bpm
        buffer.append((session_id, device, ts_utc, offset_ms, bpm))
        if len(buffer) >= 5:
            insert_hr_samples(conn, buffer)
            buffer.clear()
        w = latest.get("whoop", 0)
        f = latest.get("google_health", 0)
        typer.echo(f"\rWHOOP {w:>3} | Fitbit Air {f:>3} | Δ{abs(w - f):>3}", nl=False)

    async def _run() -> dict[str, int]:
        stop_event = asyncio.Event()
        if minutes is not None:
            async def timer() -> None:
                await asyncio.sleep(minutes * 60)
                stop_event.set()
            asyncio.create_task(timer())
        cap = asyncio.create_task(
            capture_session(addresses, start, on_sample, stop_event)
        )
        try:
            return await cap
        except asyncio.CancelledError:  # pragma: no cover
            stop_event.set()
            return await cap

    try:
        counts = asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        counts = {}

    if buffer:
        insert_hr_samples(conn, buffer)
    end_hr_session(
        conn, session_id=session_id, ended_at=datetime.now(timezone.utc).isoformat()
    )
    typer.echo(
        f"\nsession {session_id} ({label or 'unlabeled'}) done. samples: {counts}"
    )
```

> The `--minutes 0` used in tests makes the timer fire immediately, so `capture_session` returns right after the mocked `on_sample` calls; in real use omit `--minutes` and stop with Ctrl-C.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_hr_capture.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/wearable_pipeline/cli.py tests/test_cli_hr_capture.py
git commit -m "feat(ble): add hr-capture CLI command with live readout"
```

---

### Task 9: Compare — alignment + stats

**Files:**
- Create: `src/wearable_pipeline/capture/compare.py`
- Test: `tests/test_hr_compare.py`

**Interfaces:**
- Consumes: sample dicts from `load_hr_session` (Task 6) — each `{device, ts_utc, t_offset_ms, bpm}`.
- Produces:
  - `align_series(samples: list[dict], *, step_s: int = 1, max_hold_s: int = 8) -> "pandas.DataFrame"` — index = whole-second offset, columns = device names. Each device's last observed BPM is **held forward** (step interpolation) across the grid for up to `max_hold_s` seconds, then NaN until the next sample; seconds before a device's first sample are NaN. This lets a ~1 Hz series (Whoop) and a sub-1 Hz, contact-gated series (Fitbit Air) sit on one axis without dropping the slow device's points as gaps.
  - `series_rates(samples: list[dict]) -> dict` — per-device sampling stats: `{device: {n, effective_hz, median_gap_s}}`. Reports how densely each device actually streamed this session (Fitbit is expected to be much sparser than Whoop).
  - `compute_stats(aligned, rates: dict | None = None) -> dict` — keys: `mean_abs_diff`, `median_abs_diff`, `max_abs_diff`, `pearson_r`, `spearman_rho`, `pct_within_5`, `n_overlap`, and `per_device` (`{device: {avg, max, min, effective_hz, median_gap_s, n_samples}}`). Agreement stats computed over seconds where both devices have a (held) value.

**Design note (from the feasibility spike):** Fitbit Air is intentionally throttled/contact-gated — at rest it streamed ~1 sample every 2.5 s (vs Whoop's ~1 Hz), and on the wrist during motion it can be sparser still. So we treat Fitbit as the low-resolution series: step-hold it onto Whoop's 1 Hz grid rather than expecting matched samples, use a generous `max_hold_s=8`, and always report each device's effective rate so a comparison is read in the context of how much Fitbit data actually arrived.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hr_compare.py
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


def test_max_hold_expires_into_gap():
    # Fitbit sample only at t=0; with a 3 s hold it should NaN out after sec 3.
    samples = [{"device": "whoop", "ts_utc": "x", "t_offset_ms": s * 1000, "bpm": 120 + s} for s in range(10)]
    samples.append({"device": "google_health", "ts_utc": "x", "t_offset_ms": 0, "bpm": 118})
    aligned = align_series(samples, max_hold_s=3)
    stats = compute_stats(aligned)
    assert stats["n_overlap"] == 4  # seconds 0,1,2,3 held; 4..9 are gaps
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hr_compare.py -v`
Expected: FAIL (`ModuleNotFoundError: ...capture.compare`).

- [ ] **Step 3: Implement compare**

Create `src/wearable_pipeline/capture/compare.py`:

```python
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
            stats["pearson_r"] = round(float(pearsonr(a, b)[0]), 3)
            stats["spearman_rho"] = round(float(spearmanr(a, b)[0]), 3)
    return stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra analysis pytest tests/test_hr_compare.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/wearable_pipeline/capture/compare.py tests/test_hr_compare.py
git commit -m "feat(ble): add HR series alignment + agreement stats"
```

---

### Task 10: Chart render + `hr-compare` CLI command + docs

**Files:**
- Modify: `src/wearable_pipeline/capture/compare.py` (add `render_chart`)
- Modify: `src/wearable_pipeline/cli.py` (add `hr-compare`)
- Modify: `README.md` and `CLAUDE.md` (document the three commands + setup)
- Test: `tests/test_cli_hr_compare.py`

**Interfaces:**
- Consumes: `load_hr_session` (Task 6), `align_series`, `series_rates`, `compute_stats` (Task 9).
- Produces:
  - `render_chart(session_id: str, aligned, stats: dict, out_path: "pathlib.Path") -> None` — writes an overlay PNG; each series' legend label includes its effective Hz (e.g. `Fitbit Air (~0.3 Hz)`).
  - `wearable hr-compare <session_id> [--out PATH]` — prints agreement stats + per-device rate, writes the chart PNG (default `data/hr_sessions/<id>.png`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_hr_compare.py
from __future__ import annotations

from typer.testing import CliRunner

from wearable_pipeline import cli, db
from wearable_pipeline.storage import create_hr_session, insert_hr_samples

runner = CliRunner()


def _seed(conn):
    create_hr_session(
        conn, session_id="S1", label="bike",
        started_at="2026-07-21T18:30:05+00:00",
        devices=["whoop", "google_health"],
    )
    rows = []
    for sec in range(10):
        rows.append(("S1", "whoop", "x", sec * 1000, 120 + sec))
    # Fitbit sparse (every 3 s) to exercise the step-fill + rate reporting path
    for sec in range(0, 10, 3):
        rows.append(("S1", "google_health", "x", sec * 1000, 118 + sec))
    insert_hr_samples(conn, rows)


def test_hr_compare_prints_stats_and_writes_png(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    import importlib
    from wearable_pipeline import config
    importlib.reload(config)
    monkeypatch.setattr(cli, "load_settings", config.load_settings)

    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    _seed(conn)
    conn.close()

    out = tmp_path / "chart.png"
    result = runner.invoke(cli.app, ["hr-compare", "S1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "mean_abs_diff" in result.output
    assert "rate=" in result.output  # per-device effective Hz surfaced
    assert out.exists() and out.stat().st_size > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra analysis pytest tests/test_cli_hr_compare.py -v`
Expected: FAIL (`no such command 'hr-compare'`).

- [ ] **Step 3: Add `render_chart` to `compare.py`**

```python
def render_chart(session_id: str, aligned: Any, stats: dict, out_path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    label_map = {"whoop": "Whoop", "google_health": "Fitbit Air"}
    per_device = stats.get("per_device", {})
    fig, ax = plt.subplots(figsize=(11, 5), dpi=120)
    for device in aligned.columns:
        name = label_map.get(device, device)
        hz = per_device.get(device, {}).get("effective_hz")
        label = f"{name} (~{hz} Hz)" if hz is not None else name
        ax.plot(
            [i / 60.0 for i in aligned.index],
            aligned[device],
            label=label,
            lw=1.8,
        )
    ax.set_xlabel("elapsed (min)")
    ax.set_ylabel("HR (bpm)")
    ax.set_title(f"HR comparison — session {session_id}")
    ax.legend()
    subtitle = (
        f"mean|Δ|={stats.get('mean_abs_diff')}  max|Δ|={stats.get('max_abs_diff')}  "
        f"±5bpm={stats.get('pct_within_5')}%  r={stats.get('pearson_r')}  "
        f"ρ={stats.get('spearman_rho')}  n={stats.get('n_overlap')}"
    )
    ax.annotate(subtitle, xy=(0.5, -0.18), xycoords="axes fraction", ha="center")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
```

- [ ] **Step 4: Add the `hr-compare` command to `cli.py`**

```python
@app.command("hr-compare")
def hr_compare(
    session_id: str = typer.Argument(..., help="Session id (see hr_sessions.id)."),
    out: str | None = typer.Option(None, "--out", help="Chart PNG path."),
) -> None:
    """Overlay + agreement stats for a captured HR session (needs `analysis`)."""
    try:
        from .capture.compare import (
            align_series,
            compute_stats,
            render_chart,
            series_rates,
        )
    except ImportError:
        typer.echo(
            "pandas/scipy/matplotlib missing. Install with: `uv sync --extra analysis`",
            err=True,
        )
        raise typer.Exit(code=2)

    from pathlib import Path

    from .storage import load_hr_session

    settings = load_settings()
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    try:
        session, samples = load_hr_session(conn, session_id)
    except KeyError:
        typer.echo(f"No session {session_id!r}.", err=True)
        raise typer.Exit(code=1)

    aligned = align_series(samples)
    rates = series_rates(samples)
    stats = compute_stats(aligned, rates)
    for k, v in stats.items():
        if k != "per_device":
            typer.echo(f"  {k}: {v}")
    for device, d in stats["per_device"].items():
        typer.echo(
            f"  {device}: avg={d['avg']} max={d['max']} min={d['min']} "
            f"n={d.get('n_samples')} rate={d.get('effective_hz')}Hz "
            f"gap={d.get('median_gap_s')}s"
        )

    out_path = Path(out) if out else Path("data/hr_sessions") / f"{session_id}.png"
    render_chart(session_id, aligned, stats, out_path)
    typer.echo(f"wrote {out_path}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --extra analysis pytest tests/test_cli_hr_compare.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Document the feature**

In `README.md`, under Usage, add:

```markdown
### Live BLE heart-rate capture (Whoop + Fitbit Air)

Requires the `ble` extra and a one-time Bluetooth permission grant to your
terminal (System Settings → Privacy & Security → Bluetooth).

    uv sync --extra ble --extra analysis
    uv run wearable hr-scan                 # discover BLE addresses -> paste into .env
    uv run wearable hr-capture --label bike # live capture; Ctrl-C to stop
    uv run wearable hr-compare <session_id> # overlay chart + agreement stats

Oura is not supported here — its ring does not broadcast live HR over BLE.
```

In `CLAUDE.md`, add a short "BLE heart-rate capture" subsection noting: the
`capture/` subpackage is BLE-only (not part of the `WearableClient` HTTP
protocol); addresses live in `.env`; `google_health` == Fitbit Air; captures BPM
only (RR/HRV out of scope); charts write to gitignored `data/hr_sessions/`.

- [ ] **Step 7: Run the full suite**

Run: `uv run --extra dev --extra ble --extra analysis pytest`
Expected: PASS (all prior tests + the new ones).

- [ ] **Step 8: Commit**

```bash
git add src/wearable_pipeline/capture/compare.py src/wearable_pipeline/cli.py README.md CLAUDE.md tests/test_cli_hr_compare.py
git commit -m "feat(ble): add hr-compare chart+stats command and docs"
```

---

## Self-Review

**Spec coverage:**
- Two-way capture Whoop + Fitbit Air over BLE HR Profile → Tasks 2–8. ✓
- MacBook receiver, `bleak`, single central, shared clock → Task 3 (handler stamps receipt time), Task 7. ✓
- Connect by fixed `.env` address; `hr-scan` to discover → Tasks 1, 4. ✓
- Live side-by-side readout while logging → Task 8. ✓
- Stop on Ctrl-C or `--minutes` → Task 8. ✓
- Two tables `hr_sessions` + `hr_samples`, migration 0005, device vocab, ISO TEXT / INT ms → Task 6. ✓
- Compare: 1 Hz grid via step-interpolation (last value held forward up to `max_hold_s`, then gap) so the sparse contact-gated Fitbit series isn't dropped; per-device rate reporting (`series_rates`); stats (mean/median/max abs diff, Pearson, Spearman, %±5, per-device avg/max/min + effective Hz), overlay PNG to `data/hr_sessions/` with Hz in the legend → Tasks 9–10. ✓
- Extras: `ble` = bleak; matplotlib into `analysis`; per-command dependency hints → Tasks 1, 4, 8, 10. ✓
- BPM only, RR/HRV out of scope → Task 2 parser + no columns for it. ✓
- Feasibility spike as gate before full build → Task 5. ✓
- macOS Bluetooth permission documented → Tasks 5, 10. ✓
- Testing without hardware (parser, storage, compare, mocked-bleak capture path) → Tasks 2, 6, 7, 8, 9, 10. ✓
- Oura excluded → not in scope anywhere. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; commands and expected outputs are concrete. ✓

**Type consistency:** `Sample = (device, ts_utc, t_offset_ms, bpm)` is used identically in Tasks 3, 7, 8. `insert_hr_samples` tuple `(session_id, device, ts_utc, t_offset_ms, bpm)` matches between Tasks 6 and 8. `align_series`/`compute_stats` signatures match between Tasks 9 and 10. `scan_hr_peripherals`/`capture_session` names match across ble_hr and cli. ✓
