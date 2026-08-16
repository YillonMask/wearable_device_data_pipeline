# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Daily morning convention

When the user opens a session in this directory and says **"pull data"**, **"morning pull"**, **"fetch yesterday"**, or any obvious variation, do all of the following in one shot:

0. **First, remind the user to run `uv run wearable log` if they haven't already today.** The subjective 1–100 readiness must be entered *before* any device data is shown, otherwise the rating is anchored on what the wearables already concluded. If the user mentions they've already logged, skip this. If they want to log inline, also offer to enter today's Fitbit readiness from the phone app via `uv run wearable log <score> --fitbit-readiness <0-100>` (Google Health's v4 API doesn't return it).
1. Run `uv run wearable pull --date today` **and then** `uv run wearable pull --date yesterday`. Both are required:
   - **`--date today`** pulls *last night's* sleep. Every device labels a sleep with the **wake-up date**, so a sleep slept on day D−1 evening → day D morning is stored under `date=D` (today). Without this pull, the morning user can't see how they slept.
   - **`--date yesterday`** finalizes yesterday's activity row (steps, active calories), which only fully populates after midnight local once devices upload the prior day's totals.
2. Report per-device status (OK / FAIL / SKIP) for **both** days, and the key metrics from each successful row. Highlight today's `total_sleep_minutes`, `sleep_score`, `readiness_score`, and `hrv_ms` across devices side-by-side — that's the morning question the user is asking.
3. If any device fails, peek at `raw_payloads` for that device-date to see whether it's a transient API issue or a response-shape change we need to chase — `sqlite3 data/wearable.db "SELECT endpoint, substr(payload, 1, 400) FROM raw_payloads WHERE device='X' AND date='YYYY-MM-DD' ORDER BY id DESC"`.
4. If all three devices succeed and the user wants more, suggest `uv run wearable analyze` (needs `--extra analysis` installed) to see how cross-device rank correlations are shaping up.

Don't ask for permission to do step 1. The user has set up daily morning runs as a known recurring task; the explicit phrase is the consent.

## What this is

A personal, single-user (the project owner is the only user) pipeline that pulls daily health metrics from three wearables — **Oura Ring 4**, **Whoop 5.0**, and **Google Fitbit Air** — into one normalized local SQLite database, so each device's measurements of the same day can be compared honestly. Local-first; not a product for other users.

The full original spec is in `wearable-pipeline-claude-code-prompt.md` — read it before making non-trivial decisions, especially around API choices and the data model.

## Commands

```bash
uv sync --extra dev                              # runtime + dev deps (httpx, pydantic, typer, pytest, respx)
uv sync --extra dev --extra analysis             # also install pandas + scipy for `wearable analyze`
uv run pytest                                    # run the full test suite (~56 tests)
uv run pytest tests/test_oura_client.py          # one file
uv run pytest tests/test_orchestrator.py -k retry  # one test by name match

uv run wearable init                             # create the SQLite db and apply pending migrations
uv run wearable log                              # interactive: prompt for 1–100 readiness + optional Fitbit readiness
uv run wearable log 70 --fitbit-readiness 48     # one-shot: log subjective=70 and manual Fitbit readiness=48
uv run wearable auth whoop                       # interactive OAuth → writes WHOOP_REFRESH_TOKEN to .env
uv run wearable auth google                      # interactive OAuth → writes GOOGLE_HEALTH_REFRESH_TOKEN to .env
uv run wearable pull --date yesterday            # pull all devices that have credentials
uv run wearable pull --date 2026-06-12           # pull a specific day
uv run wearable backfill --since 2026-05-23      # walk every day from --since to yesterday
uv run wearable backfill --since 2026-05-23 --skip-existing  # idempotent re-run, skip complete days
uv run wearable analyze --since 2026-05-23       # Spearman rank correlation across device pairs
uv run wearable hr-import-whoop --date today     # per-sample (6s) Whoop workout HR -> hr_sessions
uv sync --extra garmin                           # install the Garmin Connect client
uv run wearable auth garmin                      # one-time SSO login (prompts MFA), caches tokens ~1yr
uv run wearable hr-import-garmin --session <id>  # merge Garmin activity HR into a BLE session

uv sync --extra viz                              # install Streamlit + pandas/scipy for the UI
uv run wearable viz                              # launch the Streamlit dashboard at http://localhost:8501
uv run wearable viz --port 8765                  # ...or pick a different port
```

`wearable pull` exit codes: 0 = all configured devices succeeded; 2 = partial failure; 1 = no configured devices or all failed. Logs go to stderr (INFO+) and to `data/logs/wearable.log` (DEBUG+, rotating).

There is no separate lint/format step configured yet — add one (ruff is a reasonable default) when the codebase grows enough to need it.

## Critical facts that override training data

These APIs are new or recently changed; do not trust training-data memory for them. Re-check the official docs before writing or modifying a client.

- **Fitbit Air uses the Google Health API** at `https://health.googleapis.com/v4/`, **not** the legacy Fitbit Web API (which is being deprecated September 2026). Do not write code against the Fitbit Web API.
- For Google Health, keep the OAuth consent screen in **testing mode** with the owner added as a test user. All required scopes are "Restricted" — testing mode is what avoids the full CASA security review for a single-user app.
- **Pull each device from its own API.** Do NOT use Google Health's Reconciled Stream or its Oura/Apple ingestion. The point of this project is to compare each device's independent numbers.
- **Whoop's data model is cycle-based, not calendar-day**: sleep cycles run sleep-to-sleep, recovery finalizes after the sleep cycle completes, day strain finalizes after midnight. When mapping into `daily_metrics`, pick a single deterministic rule for which calendar date a cycle belongs to (typically the wake-up date) and document it where you implement it.
- **Oura uses a personal access token** for single-user apps — use that, not the full OAuth flow.
- **All three APIs are daily-batch, not real-time.** Yesterday's complete data appears next morning after the device syncs. The architecture is a scheduled morning pull, not streaming ingestion.

## Architecture (the parts you need multiple files to see)

**Layered, with a strict raw-first storage rule.** Every device client returns a `FetchResult = (DailyMetrics, list[RawPayload])`. The caller (CLI/orchestrator) writes the `raw` entries to `raw_payloads` **before** upserting `metrics` into `daily_metrics`, all in a single transaction. This guarantees source data survives even if a mapping is wrong — the database, not in-memory data, is the source of truth.

- `clients/base.py` defines the `WearableClient` Protocol: `device: str` + `fetch_day(day) -> FetchResult`. Each device (`oura.py`, `whoop.py`, `google_health.py`) implements it. Adding a fourth device = new file in `clients/` + new `Literal` member in `models.Device`.
- Clients are storage-agnostic (no sqlite import). They build `RawPayload` entries during the fetch and emit them alongside the normalized `DailyMetrics`.
- `models.DailyMetrics` is the normalized shape (Pydantic v2). One row per `(date, device)`. Fields a given device does not expose stay `None` — never substitute zeros or device defaults; that destroys the comparison signal.
- `storage.upsert_daily_metrics` does `ON CONFLICT(date, device) DO UPDATE` so re-pulls overwrite cleanly. `storage.write_raw_payload` is append-only — never delete or upsert raw rows.
- `db.migrate` applies any `migrations/*.sql` files not yet present in `schema_migrations`, in lexical order, each in its own transaction. To add a schema change: drop a new `NNNN_description.sql` file in `migrations/` — do not edit prior migrations.
- `config.load_settings` reads `.env` (via python-dotenv). Missing values come back as `None`, not exceptions — clients decide whether they have what they need. Never log or echo token values.
- `cli.py` (typer) is the only entry point users invoke directly. Commands: `init`, `auth <device>`, `pull --date`, `backfill --since`. `auth`/`pull`/`backfill` are stubbed until their respective phases land.

### The schema is SQLite-now, Postgres-later

- Dates are stored as ISO-8601 TEXT (`YYYY-MM-DD`), timestamps as ISO-8601 TEXT in UTC, raw payloads as TEXT JSON. This ports to Postgres with minimal changes (raw payload column becomes JSONB).
- Don't introduce SQLite-specific types or rely on `ROWID` semantics.

### Cross-device comparison rule

**Never average `*_score` fields across devices.** Each device's 0–100 score uses a proprietary scale and algorithm — averaging them is meaningless. The intended comparison is **Spearman rank correlation** on per-device score series (planned for Phase 6's analysis script). When in doubt, prefer raw physiological values (HRV, RHR, total sleep minutes) over device scores for any comparison.

## Daily automation

Run this every morning to pull yesterday's data across all three devices:

```bash
cd /Users/xinruiyi/Documents/wearable_devices_datapipeline && uv run wearable pull --date yesterday
```

It must be run from the project directory — `.env` (credentials) and `data/wearable.db` (the database) are loaded relative to cwd. Run it between **7–10 AM PT**: Oura/Whoop/Google Health all finalize yesterday's data after midnight, and devices typically finish syncing by 6 AM.

### Option A — launchd (recommended on macOS laptops)

cron silently skips scheduled times when the laptop is asleep; launchd's `StartCalendarInterval` runs the job at the **next wake** after the scheduled time, so a closed lid never costs you a day.

The plist invokes a small shell wrapper (`scripts/morning-pull.sh`) that runs **two pulls**: `--date today` for last night's sleep and `--date yesterday` for yesterday's finalized activity. See "Daily morning convention" above for why both are needed.

1. Save this to `~/Library/LaunchAgents/com.xinruiyi.wearable-daily-pull.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xinruiyi.wearable-daily-pull</string>
    <key>WorkingDirectory</key>
    <string>/Users/xinruiyi/Documents/wearable_devices_datapipeline</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>/opt/homebrew/bin/uv run wearable pull --date today; /opt/homebrew/bin/uv run wearable pull --date yesterday</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>8</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/xinruiyi/Documents/wearable_devices_datapipeline/data/logs/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/xinruiyi/Documents/wearable_devices_datapipeline/data/logs/launchd.log</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
```

2. Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.xinruiyi.wearable-daily-pull.plist
```

3. Verify it's scheduled:
```bash
launchctl list | grep wearable
```

4. To run it once manually (useful for testing):
```bash
launchctl start com.xinruiyi.wearable-daily-pull
```

5. To stop / unload:
```bash
launchctl unload ~/Library/LaunchAgents/com.xinruiyi.wearable-daily-pull.plist
```

### Option B — cron

For a desktop that's always on, cron is simpler:
```cron
30 8 * * * cd /Users/xinruiyi/Documents/wearable_devices_datapipeline && /opt/homebrew/bin/uv run wearable pull --date today >> data/logs/cron.log 2>&1; cd /Users/xinruiyi/Documents/wearable_devices_datapipeline && /opt/homebrew/bin/uv run wearable pull --date yesterday >> data/logs/cron.log 2>&1
```

Add with `crontab -e`. cron's `PATH` is minimal — use the absolute path to `uv` (`which uv` to verify; on Apple Silicon it's typically `/opt/homebrew/bin/uv`).

### Notes & gotchas

- **Working directory matters.** Both launchd's `WorkingDirectory` and the `cd` in cron are required; without them, `dotenv` doesn't find `.env` and the database goes to the wrong place.
- **Logs**: the `wearable_pipeline` Python logger already writes to `data/logs/wearable.log` (rotating, 10 MB × 5). The launchd/cron `*.log` capture is for stdout/stderr from the process itself — useful for `uv` errors or environment failures.
- **Token rotation**: Whoop rotates refresh tokens on every call. The CLI uses `TokenManager` which persists the rotation back to `.env`, so daily runs work indefinitely. If a refresh ever fails 400 with `invalid_grant`, re-run `wearable auth whoop`. Same for Google (less commonly — Google's refresh tokens are stable unless the user revokes or the app is in testing mode and idle for 7+ days).
- **Network failures self-recover**: the orchestrator retries transient errors (429, 5xx, network timeouts) with exponential backoff. A flaky morning won't lose the day.
- **Catching up after a multi-day gap**: if you miss days (vacation, machine off), run `wearable backfill --since YYYY-MM-DD --skip-existing` — it walks every missed day and skips ones already complete.

## Build phases

The spec breaks the build into 6 phases. Phase 1 (scaffold) is done. The expectation is that each phase is verified by the owner before the next begins — don't leap ahead.

1. ✅ **Scaffold** — repo layout, deps, `.env.example`, schema + migration runner, empty client classes behind `WearableClient`.
2. ✅ **Oura client** — PAT auth, all four endpoints, raw + normalized rows; real-API verified.
3. ✅ **Whoop client** — shared `TokenManager` (rotates refresh tokens back into `.env`), interactive auth flow, cycle→wake-up-date mapping in `LOCAL_TIMEZONE`.
4. ✅ **Google Health (Fitbit Air) client** — Google OAuth in testing mode, list endpoints for daily HRV/RHR/respiratory/SpO2/sleep, `:dailyRollUp` for steps + active calories. Source-filter to Fitbit, never the reconciled stream.
5. ✅ **Orchestration** — `orchestrator.py` runs every configured device serially; per-device `try/except` for partial-failure tolerance; bounded retry on `httpx.TimeoutException`, `httpx.NetworkError`, and HTTP 429/500/502/503/504 with `Retry-After` honored; permanent 4xx propagates. Logs via stdlib `logging` to stderr + rotating file.
6. ✅ **Spearman analysis** — `wearable analyze` (requires the `analysis` extra: `pandas`, `scipy`). Per-pair Spearman rho + p across `{readiness_score, sleep_score, strain_or_activity_score, hrv_ms, resting_hr, total_sleep_minutes}`. Missing values are dropped per-pair, never imputed. No mean rho across pairs (would be meaningless).
7. ✅ **Streamlit dashboard** — `wearable viz` launches a single-page Streamlit app (`viz.py`) at `localhost:8501`. Reads `data/wearable.db` directly — no network. Sidebar filters (metric, date range, device subset), multi-device line chart for the selected metric, latest-day snapshot table, embedded Spearman correlation. `pd.DataFrame.applymap` was removed in pandas 3.0 — use `.map` (already done; flag if you see it creep back).

## OAuth (Whoop, Google Health)

- Shared infra lives in `clients/_oauth.py`:
  - `TokenManager.get_access_token()` lazily refreshes via the configured `token_endpoint`. If the response contains a new `refresh_token` (Whoop **may** rotate them — undocumented), it's persisted back to `.env` via `update_env_var`.
  - `update_env_var(path, key, value)` does an atomic, comment-preserving in-place replace (writes to `.env.tmp`, `os.replace`s into place).
- Interactive flows live in `auth/{whoop,google}_flow.py`:
  - Spin up `http.server.HTTPServer` on `127.0.0.1:8765` to capture the OAuth callback.
  - `webbrowser.open` the authorize URL; the local server's single-request handler captures `code` + validates `state`.
  - `exchange_code()` is factored out as a pure function for testability.
- **Multi-pass commands must build clients ONCE and reuse them.** Whoop rotates its refresh token on every refresh and `TokenManager` persists the new one to `.env`, but the in-memory `Settings` keeps the stale token. If a single command makes two passes that each call `enabled_clients(settings)` (e.g. daily-metrics then workouts), the second pass builds a fresh `WhoopClient` from the stale `settings` and refreshes an already-consumed token → `400 invalid_grant`. Fix/pattern: call `enabled_clients` once and pass `clients=entries` to every pass. See `cli.py:pull`, which shares entries across `pull_one_day` and `pull_workouts`.

## Google Health quirks (mostly learned from live probing — docs are sparse)

- **Scopes confirmed via the v4 discovery doc** (`https://health.googleapis.com/$discovery/rest?version=v4`): `googlehealth.activity_and_fitness.readonly`, `googlehealth.health_metrics_and_measurements.readonly`, `googlehealth.sleep.readonly`. Refresh the discovery doc to update — `auth/google_flow.py:DEFAULT_SCOPES`.
- **OAuth prompts**: `access_type=offline` + `prompt=select_account consent` in the authorize URL. `offline` guarantees a refresh token; `select_account` forces the account picker so you can switch accounts during re-auth; `consent` re-shows the scope screen so a refresh token is reliably issued every time.
- **Prerequisite — the user's Google account must be linked to Google Health.** The Fitbit app on phone must be signed into the same Google account they consent with, and the Fitbit→Google Health sync must be set up at https://fitbit.google.com/auth/signup. If not, every endpoint returns 400 with `reason: ACCOUNT_NOT_LINKED`.
- **Don't pass a `filter` query param on `dataPoints` list endpoints.** Google's AIP-160 implementation rejects every date/time syntax we tried (`INVALID_DATA_POINT_FILTER_DATA_TYPE_RESTRICTION`, `INVALID_DATA_POINT_FILTER_RESTRICTION_COMPARABLE`). Fetch a page unfiltered and filter client-side by the response's nested `date: {year, month, day}` proto.
- **`dataPoints:dailyRollUp` POST body shape**: `range` uses fields `start` and `end` (NOT `startTime`/`endTime`), each a `CivilDateTime` proto = `{date: {year, month, day}}` (NOT a flat `{year, month, day}`). Wrong shape → `Cannot find field` 400.
- **Rollup value keys are different from per-sample keys.** `Steps` per-sample has `count`; `StepsRollupValue` has `countSum`. `ActiveEnergyBurned` per-sample has `kcal`; `ActiveEnergyBurnedRollupValue` has `kcalSum`. Don't conflate the two.
- **Source filter**: real responses tag Fitbit data with `dataSource: {platform: "FITBIT"}`. `applicationDataSourceId` and other historical fields are empty. `_is_fitbit_source` keys off `platform == "FITBIT"`.
- **`dataPoints:reconcile` is the merged-source stream** — explicitly never call it. Use `:list` and filter `dataSource` client-side.
- **No Fitbit "Readiness" or "Sleep Score" in the v4 public API.** Confirmed empirically on 2026-06-22: searched every schema and property in the discovery doc — zero fields with `readiness`/`score` in their name. Probed 30+ candidate data type IDs (`daily-readiness`, `daily-readiness-score`, `fitbit-readiness`, `wellness-score`, `recovery-score`, etc.) — every one returns 400 "Invalid data type ID". The Google Health mobile app does display readiness (e.g. "48"), but it either uses an internal Google API not exposed to third-party OAuth clients, or computes the score client-side from raw inputs. Don't waste another session re-probing — `google_health.readiness_score` and `google_health.sleep_score` are permanently `None`. Same for `body_temp_deviation` and `skin_temp`.
- **`minutes` values come back as strings**, not ints — convert via `_int_or_none`. Same for `count`/`countSum`.
- **Per-session Fitbit workouts use the `exercise` data type** (NOT `activity`, `workout`, `session`, or any variant — those all return 400 "Invalid data type ID"). `fetch_workouts(since, until)` calls `GET dataTypes/exercise/dataPoints` with `pageSize=100` and filters client-side to Fitbit-source records whose start local-date falls within `[since, until]`. `provider_id` is the last path segment of the response `name` field (`name.split("/")[-1]`). `averageHeartRateBeatsPerMinute` in `metricsSummary` is a **string**, not a number — convert via `_to_float`. Calories are at `metricsSummary.caloriesKcal`. **Per-session max HR is not exposed** in the v4 API (only average HR and `heartRateZoneDurations` appear in `metricsSummary`), so `google_health` workouts always have `max_hr=None`.

## Whoop quirks (mostly learned from live probing — docs are sparse)

- **API is `/developer/v2/...` now.** `/developer/v1/cycle` still resolves for backward compat but `/developer/v1/recovery` and `/developer/v1/activity/sleep` return 404 — they only exist at v2.
- **The `offline` scope is required to get a refresh token.** Without it, the token endpoint returns only `access_token` + `expires_in`. Hardcoded in `auth/whoop_flow.py:DEFAULT_SCOPES`.
- **Refresh tokens are single-use and rotate on every refresh.** `TokenManager._refresh` persists the rotated token back to `.env`. **NEVER call the token endpoint directly with `httpx.post` for ad-hoc probes** — the rotation will be lost and the next refresh fails 400 (`invalid_grant`), requiring a full re-auth. If you must probe, instantiate a `TokenManager` and call `get_access_token()`.
- **`redirect_uri` matches `localhost` vs `127.0.0.1` strictly.** Whoop rejects mismatches with `invalid_request`. The `.env.example` ships with `127.0.0.1` to match the walkthrough.
- **Cycle → calendar day**: anchor on the sleep record whose `end` timestamp, converted to `LOCAL_TIMEZONE`, lands on the target day, and `nap == false`. The matching `recovery.sleep_id` and `cycle.id` pull the other two records. Centralized in `_pick_main_sleep` / `_end_local_date`.
- **Steps are not exposed** in the Whoop public API; `DailyMetrics.steps` stays `None` for `device='whoop'`.
- **`active_calories` is synthesized** from `cycle.score.kilojoule` (kJ / 4.184) — Whoop reports total cycle calories (which includes BMR), so the synthesized value is much larger than Google Health's "active above resting" kcal. Document the difference when comparing.
- **Sleep durations are in milliseconds** (`_milli` suffix on the field name). Convert to minutes via `_milli_to_min` (`/ 60000`).
- **Total asleep = total_in_bed - total_awake** from `stage_summary` — Whoop doesn't expose a direct "total sleep" field at this level.
- **Workouts are at `/developer/v2/activity/workout`** (`clients/whoop.py:fetch_workouts`). Per-record: `sport_name`, `start`/`end`, `score.average_heart_rate`, `score.max_heart_rate`, `score.kilojoule` (→ kcal via `_kj_to_kcal`). Unlike Google Health, Whoop **does** expose per-workout `max_heart_rate`.
- **Pagination key asymmetry (verified live 2026-06-26):** the workout list response returns the cursor under `next_token` (snake_case), but the request query param to fetch the next page is `nextToken` (camelCase). Confirmed empirically: passing `next_token` as the param does NOT advance; `nextToken` does. `fetch_workouts` reads `body["next_token"]` and sends `params["nextToken"]` — both are correct, do not "fix" the asymmetry.

### Per-sample workout HR is NOT in the public API — use the internal endpoint

- **`/developer/v2` has no heart-rate time series at all.** The full v2 endpoint list is cycle / recovery / sleep / workout / user / partner; `workout.score` tops out at `average_heart_rate`, `max_heart_rate` and `zone_durations`. The member CSV data export is aggregate-only too. Don't go looking for a `step`/`samples` param on the public workout endpoint — there isn't one.
- **The web app's own HR graph is powered by an internal endpoint**, implemented in `clients/whoop_internal.py` and exposed as `wearable hr-import-whoop --date <D>`:
  ```
  GET https://api.prod.whoop.com/metrics-service/v1/metrics/user/{user_id}
      ?name=heart_rate&start=<iso_ms>&end=<iso_ms>&step=<s>&apiVersion=7
  -> {"values": [{"time": <unix_ms>, "data": <bpm>}, ...]}
  ```
- **It authenticates against the member's account login, not OAuth.** `POST /auth-service/v2/whoop/sign-in` with `{username, password}` → `access_token` (+ `user.id`, else `GET /auth-service/v2/user`). The developer OAuth token from `whoop.py` is rejected. Credentials are `WHOOP_USERNAME` / `WHOOP_PASSWORD` in `.env`. There's no documented refresh grant — on 401 the client just re-runs sign-in.
- **6 seconds is the retrospective floor.** `step` accepts 6 / 60 / 600 only; the web app never asks for anything else and Whoop quantizes server-side. **True 1 Hz Whoop HR exists only on the live BLE broadcast** (`capture/ble_hr.py`) and cannot be recovered after the fact — so `hr-import-whoop` reports the spacing actually delivered rather than the step requested. Don't promise "per-second" from this endpoint.
- **Keep it isolated from `clients/whoop.py`.** It's unofficial, undocumented and can change without notice; a break here must never take down the daily metrics pull. It deliberately does not implement the `WearableClient` protocol.
- **Two entry points.** `--date <D>` imports each stored Whoop workout that day into its own session id `<workout_start>-whoop` (the suffix can never collide with a live BLE session started the same second); it needs the `workouts` table populated, so it depends on OAuth via `wearable pull`. `--session <hr_session_id>` needs no OAuth at all — it takes an existing BLE capture's window and merges Whoop's stored series into that same session under device key **`whoop_api`**, sitting alongside (never overwriting) the live `whoop` BLE series. That's the one to reach for when comparing against the chest-strap baseline or filling an underwater BLE dropout.
- Re-importing either way is idempotent — `hr_samples`' PK is `(session_id, device, ts_utc)` and the bucket timestamps are deterministic, so `INSERT OR IGNORE` no-ops.
- `hr-compare` derives its `max_hold_s` from each series' observed median gap (`max(8, 2 × widest gap)`) so a 6 s (or coarser) imported series doesn't step off the end of `align_series`' 8 s default hold and render as an all-NaN column. BLE-only sessions are unaffected.

## Garmin Connect (per-sample workout HR)

Garmin is the **only** source here that returns true per-second HR. Whoop's retrospective floor is a 6 s bucket and Google Health's is ~5 s; Garmin hands back the samples the device actually recorded. Implemented in `clients/garmin.py`, exposed as `wearable hr-import-garmin`.

- **Resolution is a device setting, not an API parameter.** "Every Second" recording gives 1 Hz; Garmin's default "Smart Recording" is variable and much sparser. Always report the delivered spacing (`series_rates`) rather than claiming 1 Hz.
- **`maxChartSize` silently downsamples.** `GET /activity-service/activity/{id}/details?maxChartSize=N` thins the series above `N` instead of erroring, so a long activity fetched at the 2000 default comes back quietly decimated. `_chart_size()` sizes it from the activity duration (1.25× headroom, capped at 60000). Pass `maxPolylineSize=0` — we don't want the GPS track, and it bloats the archived raw payload.
- **Detail metrics are positional.** `activityDetailMetrics[].metrics` is a bare array; `metricDescriptors[].metricsIndex` maps position → name (`directHeartRate`, `directTimestamp`), and **the layout varies by device and activity type**. Never hardcode an index — resolve from the descriptors every call. `_descriptor_index` prefers an exact key match, then falls back to a case-insensitive substring.
- **A hand-entered activity has no HR descriptor at all.** That's a legitimate answer, not an error — `_parse_details` returns `[]` and the CLI says so explicitly rather than failing obscurely.
- **Auth is Garmin Connect SSO, not OAuth.** `garminconnect>=0.3.10` does its own auth (`curl_cffi` TLS impersonation + rotating user agents) and **no longer depends on the deprecated `garth`** — don't reintroduce garth, its mobile auth flow is broken. Run `wearable auth garmin` once: it prompts for MFA and caches tokens in `GARMINTOKENS` (default `~/.garminconnect`, ~1 year). Later runs need no password, so `hr-import-garmin` stays non-interactive.
- `garminconnect` requires Python ≥3.12 while this project's floor is 3.11 — the `garmin` extra carries a `python_version >= '3.12'` marker so it doesn't make the project unresolvable on 3.11.
- **Garmin chest straps (HRM-Pro/HRM-Swim class) store HR internally while submerged** and upload afterwards, so a Garmin strap's Connect data covers underwater stretches that BLE physically cannot reach. When the same strap is also captured live over BLE as `strap`, the two series are the same sensor by different transports — expect near-identical values where both exist, and full coverage only from the API one.
- Same two entry points as `hr-import-whoop`: `--date` (own session per activity, id `<start>-garmin`) and `--session <id>` (merge into an existing BLE capture as device **`garmin`**, matching the activity by greatest time overlap). Garmin lists by *local* start date, so `--session` widens the query ±1 day to survive a session that straddles local midnight.

## Oura API gotchas (learned from Phase 2)

- **Don't query single-day windows.** `start_date == end_date` silently returns `data: []` even when records exist for that day, on all four endpoints (`/v2/usercollection/{daily_sleep,daily_readiness,daily_activity,sleep}`). The filter applies to a timezone-bearing timestamp, not the response's `day` field. Solution: always query `day-1 .. day+1` and post-filter to records where `data[].day == target.isoformat()`. This pattern is implemented in `clients/oura.py:fetch_day`.
- **`efficiency` is an integer 0..100**, not a 0..1 decimal — divide by 100 when mapping into `DailyMetrics.sleep_efficiency` (which stores 0..1). `clients/oura.py:_pct_to_unit` handles this.
- **Durations are in seconds** — convert to minutes for the normalized model (`_sec_to_min`).
- **Main sleep is the longest session.** Oura returns naps and partial sessions in `/sleep`; there's no native main-vs-nap flag. Pick `max(data, key=total_sleep_duration)` (`_longest_sleep`).

## BLE heart-rate capture

`capture/` (`ble_hr.py`, `compare.py`) is a separate, **BLE-only** subpackage —
it does not implement the `WearableClient` HTTP protocol used by `clients/`,
since it talks to devices directly over Bluetooth rather than a vendor API.

- Commands: `wearable hr-scan` (discover nearby BLE HR peripherals), `wearable
  hr-capture` (live multi-device capture into `hr_sessions`/`hr_samples`), and
  `wearable hr-compare <session_id>` (overlay chart + agreement stats; needs
  the `analysis` extra).
- Device BLE addresses live in `.env` as `WHOOP_BLE_ADDRESS` /
  `FITBIT_BLE_ADDRESS` / `STRAP_BLE_ADDRESS`, discovered via `hr-scan` —
  there's no vendor API for BLE peripheral discovery, so this is a manual
  one-time pairing step per device. `google_health` is the device key for
  Fitbit Air here too, same as the HTTP client.
- **The chest strap is the ground-truth baseline.** `STRAP_BLE_ADDRESS` is
  optional and uses device key `strap`. `hr-capture` records every configured
  BLE address concurrently (needs ≥2; `ble_hr.capture_session` already takes an
  arbitrary `{device: address}` and is partial-failure tolerant). When a
  `strap` device is present, `hr-compare` passes `baseline="strap"` to
  `compare.compute_stats`, which returns `{baseline, pairs}` — each wearable's
  agreement *against the strap* — instead of the legacy top-level single-pair
  keys. No baseline (or a pre-strap 2-device session) keeps the old flat shape.
  Adding another strap/reference = new `*_BLE_ADDRESS` in `config.py` + include
  it in the `candidates` dict in `cli.py:hr_capture`.
- **BPM only** — RR intervals / HRV are out of scope; the HR Measurement
  characteristic parser only extracts heart rate.
- **A device that disconnects mid-session keeps retrying for the rest of the
  session — it never permanently gives up while `stop_event` is unset.**
  `ble_hr.capture_session`'s `run_device` retries at a fast 1s interval for
  the first `reconnect_attempts` failures, then backs off to
  `retry_backoff_s` (default 10s) so a device that's genuinely unreachable
  for a long stretch (e.g. a swimmer out of BLE range underwater) doesn't
  hammer the BLE stack or spam logs — but it keeps trying, so a wall touch or
  resurfacing mid-swim picks the stream back up automatically instead of
  leaving that device dead for the rest of the session. The backoff sleep
  itself is interruptible (`asyncio.wait_for` on `stop_event`), so stopping a
  session never stalls waiting out another device's backoff.
- **BLE physically cannot reach a submerged/distant peripheral** — the
  retry logic above only recovers gaps near the surface (wall touches, flip
  turns); it can't fix genuine underwater dead zones. For full continuous-swim
  HR, the only path is a retrospective pull from Google Health API v4's
  `heart-rate` data type via `dataTypes/heart-rate/dataPoints:list` (distinct
  from the `daily-resting-heart-rate` type already used) — official,
  per-sample, ~5s median interval in practice — sliced to the workout's
  start/end window from `fetch_workouts`, since Fitbit stores that data
  on-device/cloud-side regardless of BLE range. Not yet implemented in
  `clients/google_health.py`. **The Whoop half of this is implemented** —
  `wearable hr-import-whoop` pulls Whoop's own stored series for a workout
  window at 6 s resolution (see "Per-sample workout HR" above), which covers
  the underwater stretch BLE missed; Fitbit is the remaining gap.
- **Fitbit only streams while its "Share Heart Rate" toggle is actively live**
  in the Fitbit/Google Health phone app, and it's a low-rate, contact-gated
  source — roughly 1 sample/2.5 s at rest, sometimes sparser on the wrist
  during motion — versus Whoop's steady ~1 Hz. `capture/compare.py` handles
  this by step-interpolating the sparser series onto a shared 1 s grid
  (holding the last value forward up to `max_hold_s`) and reports each
  device's effective Hz (`series_rates`) alongside the agreement stats, so a
  slow Fitbit rate is visible rather than silently smoothed away.
- Chart PNGs from `hr-compare` write to `data/hr_sessions/<session_id>.png` by
  default — under the gitignored `data/` directory, same as the SQLite DB and
  logs.
- Requires a one-time macOS Bluetooth permission grant to the terminal app
  (System Settings → Privacy & Security → Bluetooth) before `hr-scan` /
  `hr-capture` can see peripherals.
- Oura is out of scope for BLE capture — the ring does not broadcast live HR
  over BLE.

## Working norms

- Before writing or modifying a device client, fetch that device's current official API docs (endpoint shapes, scopes, auth steps). Training-data memory of these APIs is stale.
- Ask before assuming anything about credentials or environment.
- Keep changes small and scoped to the current phase so the owner can verify as you go.
- Never hardcode tokens. All secrets live in `.env`; `.env.example` is the committed template.
