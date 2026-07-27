# wearable-devices-datapipeline

A personal, local-first pipeline that pulls daily health metrics from three wearables —
**Oura Ring 4**, **Whoop 5.0**, and **Fitbit Air** (via the Google Health API) — into one
normalized SQLite database, so each device's measurement of the *same day* can be compared
honestly.

Each device runs a proprietary algorithm over its own sensors. Pulling every device from its
**own** API (never a merged/reconciled stream) lets you ask questions like: *do the three
devices agree on how I slept? Whose "readiness" score actually tracks how I feel?*

> Single-user, local-first. Not a hosted product — it reads/writes a local `.env` and SQLite DB.

## What it does

- **Independent per-device pulls** — Oura (personal access token), Whoop (OAuth2, cycle-based
  model mapped to wake-up date), Fitbit Air (Google Health v4 API, source-filtered to Fitbit).
- **Raw-first storage** — every raw API payload is written to `raw_payloads` *before* the
  normalized row is upserted into `daily_metrics`, in one transaction. The database is the
  source of truth, so source data survives even a bad mapping.
- **Normalized model** — one row per `(date, device)`. Fields a device doesn't expose stay
  `NULL` (never zero-filled — that would destroy the comparison signal).
- **Partial-failure tolerant orchestration** — devices run serially; a per-device try/except
  plus bounded retry (with `Retry-After` honored) means one flaky API won't lose the day.
- **Spearman rank-correlation analysis** — compares device score/metric *series* by rank.
  Device 0–100 scores are never averaged across devices (different proprietary scales); raw
  physiological values (HRV, resting HR, total sleep minutes) are preferred for comparison.
- **Streamlit dashboard** — a local single-page UI over the SQLite DB (no network).

## Data model

`daily_metrics` — normalized, one row per `(date, device)`:

| group | fields |
|---|---|
| sleep | `total_sleep_minutes`, `sleep_efficiency`, `sleep_latency_minutes`, `rem/deep/light/awake_minutes`, `sleep_score` |
| recovery | `readiness_score`, `hrv_ms`, `resting_hr`, `respiratory_rate`, `body_temp_deviation`, `skin_temp` |
| load | `strain_or_activity_score`, `active_calories`, `steps` |

Dates are ISO-8601 `TEXT`, timestamps ISO-8601 UTC, raw payloads JSON `TEXT` — a schema that
ports to Postgres with minimal changes.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev                    # runtime + dev deps
uv sync --extra dev --extra analysis   # also pandas + scipy (for `analyze`)
uv sync --extra viz                    # also Streamlit (for `viz`)

cp .env.example .env                   # then fill in credentials
uv run wearable init                   # create the DB, apply migrations
```

Credentials all live in `.env` (git-ignored); `.env.example` is the committed template.

- **Oura** uses a personal access token.
- **Whoop** and **Google Health** use interactive OAuth:
  ```bash
  uv run wearable auth whoop
  uv run wearable auth google
  ```

## Usage

```bash
uv run wearable pull --date yesterday          # pull all configured devices for a day
uv run wearable pull --date 2026-06-12         # a specific day
uv run wearable backfill --since 2026-05-23     # walk every day up to yesterday
uv run wearable backfill --since 2026-05-23 --skip-existing   # idempotent re-run
uv run wearable analyze --since 2026-05-23      # Spearman rank correlation across device pairs
uv run wearable viz                             # launch the Streamlit dashboard
```

`pull` exit codes: `0` all configured devices succeeded · `2` partial failure · `1` no
configured devices or all failed.

### Live BLE heart-rate capture (Whoop + Fitbit Air, + optional chest-strap baseline)

The cloud APIs only return workout *summaries* (avg/max HR, duration, calories).
To compare the devices **second-by-second** during a workout, this captures
their live heart-rate broadcast directly over Bluetooth LE — your Mac acts as a
single receiver connected to every band at once, so all streams share one clock
and line up automatically.

> **Add a chest strap as a ground-truth baseline.** A standard BLE HR strap
> (Polar H10, Wahoo TICKR, …) is the accepted reference for HR accuracy. Set
> `STRAP_BLE_ADDRESS` and it's captured alongside the wearables; `hr-compare`
> then reports each wearable's error *against the strap* instead of just
> wearable-vs-wearable. The strap is optional — without it, capture/compare
> behave exactly as before.

> Oura is **not** supported here — its ring doesn't broadcast live HR over BLE.
> Stationary workouts only (indoor bike, treadmill, rower) — Bluetooth range is
> ~10 m, so keep the Mac nearby.

#### One-time setup

1. **Install the extras** (`ble` for capture, `analysis` for the comparison chart):

   ```bash
   uv sync --extra ble --extra analysis
   ```

2. **Grant your terminal Bluetooth permission** — macOS blocks BLE otherwise:
   System Settings → Privacy & Security → **Bluetooth** → enable your terminal
   app (Terminal / iTerm), then restart it.

3. **Turn on each device's HR broadcast:**
   - **Whoop:** app → Device Settings → **HR Broadcast → ON**.
   - **Fitbit Air:** Google Health app → Connections → **Share Heart Rate**, and
     hold the device near the Mac. (Fitbit only streams *while this is active* —
     see the note below.)
   - **Chest strap (optional):** wet the electrodes and put it on — most straps
     broadcast standard BLE HR automatically once worn.

4. **Discover the BLE addresses and save them to `.env`:**

   ```bash
   uv run wearable hr-scan
   ```

   With the broadcasts on, this lists nearby HR peripherals with their
   addresses. Copy each into `.env` (they're long CoreBluetooth UUIDs on macOS):

   ```bash
   WHOOP_BLE_ADDRESS=93F56490-....
   FITBIT_BLE_ADDRESS=240EA590-....
   STRAP_BLE_ADDRESS=1C4E9A20-....   # optional ground-truth baseline
   ```

#### Capturing a session

Make sure Whoop **HR Broadcast** and Fitbit **Share Heart Rate** are both live,
then:

```bash
uv run wearable hr-capture --label bike        # capture until you stop it
uv run wearable hr-capture --label bike --minutes 30   # ...or auto-stop after 30 min
```

A live line shows every connected device so you can confirm each is streaming.
Whichever BLE addresses are set are captured; you need at least two. When a
strap is present it's shown first and each wearable is annotated with its delta
vs the strap:

```
Strap 140 | WHOOP 142 (Δ  2) | Fitbit 138 (Δ  2)
```

Whoop and the strap update ~1×/second; Fitbit Air is slower (~1 sample every
2–3 s) and only while its Share Heart Rate is active — if it shows `0`, re-toggle
the share. The session runs from launch until you **press Ctrl-C** (or
`--minutes` elapses); there is no automatic workout detection. On stop it prints
the session id and per-device sample counts:

```
session 20260721T203005Z (bike) done. samples: {'strap': 605, 'whoop': 312, 'google_health': 118}
```

#### Comparing the curves

```bash
uv run wearable hr-compare 20260721T203005Z    # use the id printed above
```

This writes an overlay chart to `data/hr_sessions/<session_id>.png` and prints
agreement stats (mean/median/max |Δ|, % within ±5 bpm, Pearson/Spearman) plus
each device's effective sampling rate. **When the session includes a strap, it's
the baseline** (drawn in black on the chart) and the stats are reported per
wearable *against the strap* — e.g. `whoop vs strap` and `google_health vs
strap`. Without a strap, a two-device session reports the single pairwise
agreement as before. Because Fitbit samples slower than the others, its series
is step-interpolated onto the shared 1 Hz grid rather than dropped, and its true
rate is reported alongside the stats.

Data lands in the existing SQLite DB (`hr_sessions` + `hr_samples`); charts go
under the git-ignored `data/` directory.

## Tests

```bash
uv run pytest                          # full suite
uv run pytest tests/test_oura_client.py
```

HTTP is mocked with `respx`; no live API calls in the test suite.

## Architecture

Layered, storage-agnostic clients behind a small protocol:

```
cli.py ── orchestrator.py ──▶ clients/{oura,whoop,google_health}.py ──▶ FetchResult
                     │                                                (DailyMetrics, [RawPayload])
                     ▼
              storage.py ──▶ SQLite (raw_payloads first, then daily_metrics)
```

- `clients/base.py` — the `WearableClient` protocol: `device: str` + `fetch_day(day) -> FetchResult`.
  Adding a device = a new file in `clients/` + a new member in `models.Device`.
- `clients/_oauth.py` — shared `TokenManager` (lazy refresh; persists rotated refresh tokens
  back to `.env`) and interactive OAuth flows.
- `db.migrate` applies `migrations/*.sql` files not yet recorded in `schema_migrations`, each in
  its own transaction. Add a change by dropping a new `NNNN_description.sql` file — existing
  migrations are never edited.

## License

Personal project — no license granted. All rights reserved unless stated otherwise.
