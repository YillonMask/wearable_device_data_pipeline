# Claude Code Starting Prompt — Multi-Wearable Health Data Pipeline

> Paste everything below this line into Claude Code.

---

## What I'm building

A personal data pipeline that pulls my daily health metrics from three wearables — **Oura Ring 4**, **Whoop 5.0**, and **Google Fitbit Air** — into one normalized local database, so I can compare how each device measures my sleep, recovery, and training load over time. Single user (me), local-first, not a product for other users.

## Critical facts to get right (do NOT rely on your training data for these — verify against current official docs)

- **Fitbit Air uses the Google Health API, NOT the legacy Fitbit Web API.** The Fitbit Web API is being deprecated in September 2026 — do not build on it. Base URL is `https://health.googleapis.com/v4/`. Auth is Google OAuth 2.0; the project is registered in Google Cloud Console. All scopes are "Restricted" — for a personal single-user app, keep the OAuth consent screen in **testing mode** with myself added as a test user, so I don't need the full CASA security review.
- **Whoop** uses OAuth 2.0 (authorization-code flow). Its data model is **cycle-based** (sleep cycle → recovery cycle → strain cycle), organized sleep-to-sleep rather than by calendar day. Recovery is finalized after a sleep cycle completes; day strain finalizes after midnight. Account for this when joining against calendar-day data from the other two.
- **Oura** uses OAuth 2.0 but supports a **personal access token** for single-user apps — use that, it's the simplest path. Up to ~2 years of history, queryable by date range.
- **All three are daily-batch, not real-time streams.** Yesterday's complete data is available the next morning after the device syncs. The architecture is a **scheduled morning pull**, not streaming ingestion.
- **Pull each device from its OWN API.** Do NOT use Google Health's "Reconciled Stream" or its Oura/Apple ingestion — I want each device's independent numbers so I can compare them honestly.

## Tech stack

- Python 3.11+
- SQLite to start, but design the schema so I can swap to Postgres later
- `httpx` for API calls, `pydantic` for data models, `python-dotenv` for secrets, `typer` for a small CLI
- CLI commands like `auth`, `pull --date yesterday`, `backfill --since YYYY-MM-DD`
- Secrets in `.env` with a committed `.env.example`; never hardcode tokens

## Data model

- One normalized `daily_metrics` table keyed by `(date, device)`.
- A separate `raw_payloads` table storing each device's raw JSON response, so I never lose source data even if my normalization is wrong.
- Normalized comparable fields:
  - **sleep:** total_sleep_minutes, sleep_efficiency, sleep_latency_minutes, rem_minutes, deep_minutes, light_minutes, awake_minutes, sleep_score (0–100 where available)
  - **recovery/readiness:** readiness_score (0–100), hrv_ms, resting_hr, respiratory_rate, body_temp_deviation (Oura), skin_temp (Whoop)
  - **load:** strain_or_activity_score, active_calories, steps
- **Do NOT average scores across devices** — they use different scales and proprietary algorithms and are not interchangeable. Keep each device's raw scores separate and add a code comment noting this.

## Build it in phases (let me verify each before moving to the next)

1. **Scaffold:** repo layout, dependencies, `.env.example`, SQLite schema + a migration step, and empty client classes for each device behind a common `WearableClient` protocol/interface.
2. **Oura client first** (simplest auth): personal access token → pull one day → write raw + normalized rows. Get this working end to end before anything else.
3. **Whoop client:** OAuth flow, handle the cycle-based date model, map into the same normalized schema.
4. **Google Health client (Fitbit Air):** Google OAuth in testing mode, pull the daily data types (Daily HRV, Daily Resting Heart Rate, Daily SpO2, Daily Respiratory Rate, sleep summary, steps).
5. **Orchestration:** a `pull --date yesterday` command that runs all three plus a `backfill` command, with basic retry and logging.
6. **Analysis script:** load the table into pandas and compute **Spearman rank correlation** between the three readiness/recovery scores (since absolute values aren't comparable, rank agreement is the meaningful signal).

## How to work with me

- Before writing each device client, check that client's current official API docs for exact endpoint shapes, scopes, and auth steps — these APIs are new or recently changed and your training data may be stale.
- Ask me before assuming anything about my credentials or environment.
- Keep changes small and explain what each phase does so I can follow along and verify as we go.
