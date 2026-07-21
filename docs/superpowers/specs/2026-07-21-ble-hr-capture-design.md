# Live BLE heart-rate capture + sec-by-sec comparison — design

**Date:** 2026-07-21
**Status:** Approved (design); ready for implementation planning

## Goal

Capture **live heart rate** from **Whoop 5.0** and **Fitbit Air** simultaneously
during a workout, over Bluetooth LE (the standard Heart Rate Profile), and
compare the two devices **second-by-second**.

This exists because the official cloud APIs are daily-batch and expose only
workout *summaries* (avg HR, max HR, duration, calories) — never a real-time or
high-resolution HR stream. The only path to a granular comparison is to capture
the devices' live BLE HR broadcast ourselves.

This is a new, streaming data path. It does **not** fold into `daily_metrics`
(daily aggregates) or `workouts` (per-session API summaries) — it gets its own
tables and its own capture command.

## Why only these two devices

- **Fitbit Air** — broadcasts real-time HR over the standard Bluetooth Heart Rate
  Profile (confirmed via Google Health support docs; tested with Peloton, Zwift,
  Concept2). Caveat: uses a **proximity-based selection** flow, not a normal
  pairing dialog.
- **Whoop 5.0** — a generic BLE HR monitor; enable **HR Broadcast** in Device
  Settings (confirmed via Whoop support docs).
- **Oura Ring 4 — excluded.** Oura's own docs confirm the ring does **not**
  broadcast HR; Live Activity Tracking instead pairs an *external* monitor *into*
  the Oura app. The owner also does not wear Oura during workouts. A true
  three-way sec-by-sec comparison is therefore impossible; this tool is two-way.

## Decisions (resolved during brainstorming)

- **Receiver:** the **MacBook**, running Python + `bleak` as a single BLE central
  that holds both peripheral connections at once. Workouts are **stationary**
  (indoor bike, treadmill) within ~10 m BLE range, at the gym with the laptop
  present.
- **Shared clock:** one central timestamps both streams at receipt, so the two
  series are automatically aligned — no cross-device clock-skew correction needed.
- **Capture UX:** manual start (`hr-capture`), stop on **Ctrl-C** or optional
  `--minutes N`. **Live terminal readout** of both devices' current BPM
  side-by-side (doubles as a "both still connected?" check), while logging.
- **Device identity:** connect by **fixed BLE address** stored in `.env`
  (`WHOOP_BLE_ADDRESS`, `FITBIT_BLE_ADDRESS`), populated once via `hr-scan`.
  Addresses are more reliable than advertised-name matching (generic names, gym
  clashes). Addresses are not secrets but live in `.env` for single-file config.
- **Output:** `hr-compare <session>` produces an **overlay chart + agreement
  stats**.
- **Scope cut (YAGNI):** the HR Measurement characteristic can also carry
  **RR-intervals** (for HRV) and energy-expended. These are **out of scope for
  v1** — capture BPM only. No reserved columns.

## Open risk — feasibility spike is implementation step 1

Whether **Fitbit Air's proximity-selection "Share HR" flow will connect to a
laptop BLE central** (rather than to gym equipment) is **unverified**. Whoop's
generic broadcast is lower-risk but also unproven on this Mac.

The implementation plan's **first step is a manual spike**: run `hr-scan`,
confirm both devices appear, then a minimal connect-and-read confirming the Mac
receives notifications from **each**. If Fitbit Air will not connect to a laptop
central, **stop and report back** — do not build the full tool on a broken
foundation. (Consistent with the project norm of empirically probing new
device/API behavior before building on it.)

macOS prerequisite: the terminal app must be granted **Bluetooth** permission in
System Settings → Privacy & Security → Bluetooth, or `bleak` errors on start.

## Architecture

New subpackage `src/wearable_pipeline/capture/` — BLE is a different protocol from
the HTTP `clients/`, so it is **not** forced into the `WearableClient`
(`fetch_day()`) protocol.

- `ble_hr.py` — async `bleak` core.
  - `scan_hr_peripherals()` → list of (name, address) for nearby peripherals
    advertising the HR service (`0x180D`).
  - `capture_session(addresses, on_sample, stop_event)` → connects each address,
    `start_notify` on the HR Measurement characteristic (`0x2A37`), decodes each
    notification, invokes `on_sample(device, ts, bpm)`.
  - `parse_hr_measurement(data: bytes) -> int` → per BLE spec: byte 0 is flags;
    bit 0 selects uint8 (1 byte) vs uint16 (2 bytes, little-endian) BPM. RR/energy
    fields are parsed past but discarded in v1.
- `compare.py` — load a session's samples, align to a 1 Hz grid, compute stats,
  render the overlay chart.
- Storage helpers (in `storage.py`, reusing the existing `db` connection module):
  `create_hr_session`, `insert_hr_samples` (batched), `end_hr_session`,
  `load_hr_session`.
- CLI commands in `cli.py`: `hr-scan`, `hr-capture`, `hr-compare`.

### Resilience

Mirrors the orchestrator's partial-failure ethos: if one device disconnects,
retry a bounded number of times and log the gap while the **other** device keeps
logging. One dropped connection never aborts the session.

## Data model — migration `0005_hr_capture.sql`

```sql
CREATE TABLE hr_sessions (
    id          TEXT PRIMARY KEY,   -- ISO-8601 UTC start, e.g. 2026-07-21T18:30:05Z
    label       TEXT,               -- 'bike' | 'treadmill' | ...
    started_at  TEXT NOT NULL,      -- ISO-8601 UTC
    ended_at    TEXT,               -- ISO-8601 UTC; NULL until stopped
    devices     TEXT                -- JSON array, e.g. ["whoop","google_health"]
);

CREATE TABLE hr_samples (
    session_id  TEXT NOT NULL,      -- FK -> hr_sessions.id
    device      TEXT NOT NULL,      -- 'whoop' | 'google_health'
    ts_utc      TEXT NOT NULL,      -- receipt time, ISO-8601 UTC
    t_offset_ms INTEGER NOT NULL,   -- ms since session start (alignment / plotting)
    bpm         INTEGER NOT NULL,
    PRIMARY KEY (session_id, device, ts_utc)
);

CREATE INDEX idx_hr_samples_session ON hr_samples (session_id, device);
```

- Reuses the existing device vocabulary (`whoop`, `google_health`); `hr-compare`
  renders `google_health` as "Fitbit Air".
- ISO-8601 TEXT dates/timestamps, integer offsets — same SQLite-now/Postgres-later
  discipline as the rest of the schema. New migration file only; prior migrations
  untouched.
- Samples are event-driven (logged as each notification arrives, ~1 Hz), not
  polled; actual receipt timestamps are stored, so a device emitting slightly
  off-1 Hz is represented faithfully.

## Capture flow — `wearable hr-capture --label "bike" [--minutes N]`

1. Read `WHOOP_BLE_ADDRESS` / `FITBIT_BLE_ADDRESS` from `.env`; if missing, error
   clearly and point to `hr-scan`.
2. Create the `hr_sessions` row (`id` = current UTC timestamp).
3. Connect both addresses via `bleak`; `start_notify` on `0x2A37`.
4. Per notification: decode BPM → stamp `ts_utc` + `t_offset_ms` → buffer → flush
   to `hr_samples` ~once/second → refresh the live line:
   `WHOOP 142 | Fitbit Air 138 | Δ4 | 03:12 | samples W192 F190`.
5. Stop on **Ctrl-C** (asyncio signal handler) or when `--minutes` elapses:
   `stop_notify`, disconnect, set `ended_at`, final flush, print a summary
   (duration, per-device sample counts, any reconnect gaps).

## Compare — `wearable hr-compare <session>`

- Build a common 1 Hz grid from `t_offset_ms`; for each second, take the nearest
  sample within **2 s**, else mark a gap. Never impute across long gaps.
- Stats over overlapping seconds: **mean / median / max absolute difference,
  Pearson r, Spearman ρ, % of time within ±5 BPM**, plus each device's
  avg / max / min.
- Chart: both curves overlaid vs elapsed minutes, stats annotated; PNG saved to
  `data/hr_sessions/<id>.png` (under the gitignored `data/`).

## Dependencies

- New extra `ble = ["bleak"]` — capture only.
- Add `matplotlib` to the existing `analysis` extra — used by `hr-compare`
  (which already needs pandas/scipy).
- `hr-capture` requires `ble`; `hr-compare` requires `analysis`. Each command
  errors with the exact `uv sync --extra …` hint if its dependency is missing.

## Testing (no hardware in CI)

- `parse_hr_measurement`: unit tests for uint8 and uint16 formats, and payloads
  with trailing RR/energy bytes (must ignore them).
- Storage: create/insert/end against a temp DB (existing fixture pattern).
- `compare`: alignment + stats on synthetic two-series data, including a gap.
- A mocked-`bleak` test exercising the notification → store → display path with no
  radio.
- Real-hardware BLE connectivity is covered by the **manual spike**, not automated
  tests.

## Out of scope (v1)

- RR-intervals / HRV and energy-expended fields from the HR characteristic.
- Oura (cannot broadcast).
- Non-stationary / phone-based capture (a future Android-receiver path was
  considered and deferred).
- Joining captured sessions to the API `workouts` summaries.
- Live plotting during capture (live *text* readout only).
