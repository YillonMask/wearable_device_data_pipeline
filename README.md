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

### Live BLE heart-rate capture (Whoop + Fitbit Air)

Requires the `ble` extra and a one-time Bluetooth permission grant to your
terminal (System Settings → Privacy & Security → Bluetooth).

    uv sync --extra ble --extra analysis
    uv run wearable hr-scan                 # discover BLE addresses -> paste into .env
    uv run wearable hr-capture --label bike # live capture; Ctrl-C to stop
    uv run wearable hr-compare <session_id> # overlay chart + agreement stats

Oura is not supported here — its ring does not broadcast live HR over BLE.

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
