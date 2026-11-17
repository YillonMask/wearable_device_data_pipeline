from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import get_args

import httpx
import typer

from . import db
from .auth.google_flow import run_interactive_flow as run_google_flow
from .auth.whoop_flow import run_interactive_flow as run_whoop_flow
from .capture.ble_hr import capture_session, scan_hr_peripherals
# series_rates is stdlib-only (pandas/scipy live behind lazy imports in the
# other compare helpers), so it is safe to import without the `analysis` extra.
from .capture.compare import series_rates
from .clients._oauth import update_env_var
from .clients.garmin import GarminAuthError, GarminClient, activity_start_utc
from .clients.google_health import GoogleHealthClient
from .clients.whoop_internal import VALID_STEPS, WhoopInternalClient
from .config import load_settings
from .logging_setup import configure_logging
from .models import Device
from .orchestrator import (
    backfill as orchestrate_backfill,
    enabled_clients,
    exit_code_for,
    pull_one_day,
    pull_workouts,
    summarize,
)
from .self_report_inbox import (
    InboxError,
    default_since as default_inbox_since,
    render_script as render_inbox_script,
    sync as sync_inbox,
)
from .storage import (
    add_hr_session_device,
    create_hr_session,
    end_hr_session,
    hr_session_exists,
    insert_hr_samples,
    load_hr_session,
    load_workouts,
    upsert_manual_readiness,
    upsert_self_report,
    write_raw_payload,
)

app = typer.Typer(help="Multi-wearable health data pipeline.")


@app.command("init")
def init_db() -> None:
    """Create the database and apply pending migrations."""
    settings = load_settings()
    conn = db.connect(settings.database_path)
    applied = db.migrate(conn)
    if applied:
        typer.echo(f"Applied migrations: {', '.join(applied)}")
    else:
        typer.echo("Database already up to date.")


@app.command()
def auth(device: str) -> None:
    """Run the auth flow for a device (whoop | google | garmin).

    Oura uses a personal access token — set OURA_PERSONAL_ACCESS_TOKEN in
    .env directly; no auth command needed. `garmin` is Garmin Connect SSO
    (not OAuth): it caches tokens on disk and may prompt for an MFA code.
    """
    settings = load_settings()
    if device == "oura":
        typer.echo(
            "Oura uses a personal access token. Generate one at "
            "https://cloud.ouraring.com/personal-access-tokens and set "
            "OURA_PERSONAL_ACCESS_TOKEN in .env."
        )
        raise typer.Exit(code=0)
    if device == "whoop":
        if not settings.whoop_client_id or not settings.whoop_client_secret:
            typer.echo(
                "WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET missing from .env.",
                err=True,
            )
            raise typer.Exit(code=2)
        refresh = run_whoop_flow(
            client_id=settings.whoop_client_id,
            client_secret=settings.whoop_client_secret,
            redirect_uri=settings.whoop_redirect_uri
            or "http://127.0.0.1:8765/callback",
        )
        update_env_var(settings.env_path, "WHOOP_REFRESH_TOKEN", refresh)
        typer.echo("Whoop refresh token written to .env.")
        raise typer.Exit(code=0)
    if device == "google":
        if not settings.google_client_id or not settings.google_client_secret:
            typer.echo(
                "GOOGLE_HEALTH_CLIENT_ID / GOOGLE_HEALTH_CLIENT_SECRET "
                "missing from .env.",
                err=True,
            )
            raise typer.Exit(code=2)
        refresh = run_google_flow(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=settings.google_redirect_uri
            or "http://127.0.0.1:8765/callback",
        )
        update_env_var(settings.env_path, "GOOGLE_HEALTH_REFRESH_TOKEN", refresh)
        typer.echo("Google Health refresh token written to .env.")
        raise typer.Exit(code=0)
    if device == "garmin":
        if not settings.garmin_email or not settings.garmin_password:
            typer.echo(
                "GARMIN_EMAIL / GARMIN_PASSWORD missing from .env.", err=True
            )
            raise typer.Exit(code=2)
        client = GarminClient(
            email=settings.garmin_email,
            password=settings.garmin_password,
            token_dir=settings.garmin_token_dir,
        )
        try:
            # Any authenticated call forces the SSO handshake; the MFA prompt
            # (if the account has it on) needs a terminal, which is exactly why
            # this is a separate command rather than inline in hr-import-garmin.
            client.list_activities(date.today())
        except (ImportError, GarminAuthError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2)
        typer.echo(
            f"Garmin tokens cached in {settings.garmin_token_dir} (valid ~1 year). "
            "Later runs need no password."
        )
        raise typer.Exit(code=0)
    typer.echo(f"auth flow for '{device}' not implemented yet.", err=True)
    raise typer.Exit(code=1)


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


@app.command("hr-capture")
def hr_capture(
    label: str | None = typer.Option(None, "--label", help="Session label, e.g. 'bike'."),
    minutes: float | None = typer.Option(
        None, "--minutes", help="Auto-stop after N minutes (else Ctrl-C)."
    ),
) -> None:
    """Capture live HR from Whoop + Fitbit Air (+ optional chest strap) over BLE.

    A configured STRAP_BLE_ADDRESS is captured as the ground-truth baseline
    alongside the wearables; `hr-compare` then reports each wearable's error
    against it. Devices are captured concurrently (partial-failure tolerant).
    """
    settings = load_settings()
    # `strap` first so it reads as the reference in the live status line and
    # the stored device order. Include only devices with a configured address.
    candidates = {
        "strap": settings.strap_ble_address,
        "whoop": settings.whoop_ble_address,
        "google_health": settings.fitbit_ble_address,
    }
    addresses = {device: addr for device, addr in candidates.items() if addr}
    if len(addresses) < 2:
        typer.echo(
            "Need at least two BLE addresses to compare. Set STRAP_BLE_ADDRESS "
            "plus WHOOP_BLE_ADDRESS / FITBIT_BLE_ADDRESS in .env. "
            "Run `wearable hr-scan` to discover them.",
            err=True,
        )
        raise typer.Exit(code=2)

    configure_logging()
    conn = db.connect(settings.database_path)
    db.migrate(conn)

    start = datetime.now(timezone.utc)
    session_id = start.strftime("%Y%m%dT%H%M%SZ")
    devices = list(addresses.keys())
    create_hr_session(
        conn,
        session_id=session_id,
        label=label,
        started_at=start.isoformat(),
        devices=devices,
    )
    latest: dict[str, int] = {}
    buffer: list[tuple[str, str, str, int, int]] = []
    sample_counts: dict[str, int] = {}

    status_labels = {"strap": "Strap", "whoop": "WHOOP", "google_health": "Fitbit"}

    def on_sample(sample) -> None:
        device, ts_utc, offset_ms, bpm = sample
        latest[device] = bpm
        buffer.append((session_id, device, ts_utc, offset_ms, bpm))
        sample_counts[device] = sample_counts.get(device, 0) + 1
        if len(buffer) >= 5:
            insert_hr_samples(conn, buffer)
            buffer.clear()
        # Live line: show each device's latest bpm; when a strap baseline is
        # present, annotate each wearable with its delta vs the strap.
        base = latest.get("strap")
        parts = []
        for d in devices:
            val = latest.get(d, 0)
            name = status_labels.get(d, d)
            if d != "strap" and base is not None:
                parts.append(f"{name} {val:>3} (Δ{abs(val - base):>3})")
            else:
                parts.append(f"{name} {val:>3}")
        typer.echo("\r" + " | ".join(parts), nl=False)

    async def _run() -> dict[str, int]:
        stop_event = asyncio.Event()
        if minutes is not None:
            async def timer() -> None:
                await asyncio.sleep(minutes * 60)
                stop_event.set()
            timer_task = asyncio.create_task(timer())
        cap = asyncio.create_task(
            capture_session(addresses, start, on_sample, stop_event)
        )
        try:
            return await cap
        except asyncio.CancelledError:  # pragma: no cover
            stop_event.set()
            return await cap

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass

    if buffer:
        insert_hr_samples(conn, buffer)
    end_hr_session(
        conn, session_id=session_id, ended_at=datetime.now(timezone.utc).isoformat()
    )
    typer.echo(
        f"\nsession {session_id} ({label or 'unlabeled'}) done. "
        f"samples: {sample_counts}"
    )


@app.command("hr-compare")
def hr_compare(
    session_id: str = typer.Argument(..., help="Session id (see hr_sessions.id)."),
    out: str | None = typer.Option(None, "--out", help="Chart PNG path."),
    baseline: str | None = typer.Option(
        None,
        "--baseline",
        help="Device to treat as ground truth (default: strap, else garmin).",
    ),
) -> None:
    """Overlay + agreement stats for a captured HR session (needs `analysis`)."""
    from pathlib import Path

    settings = load_settings()
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    try:
        _, samples = load_hr_session(conn, session_id)
    except KeyError:
        typer.echo(f"No session {session_id!r}.", err=True)
        raise typer.Exit(code=1)

    # `align_series`/`compute_stats`/`render_chart` lazily import
    # pandas/scipy/matplotlib inside their own bodies (Task 9's design), so
    # the ImportError for a missing `analysis` extra only surfaces once one
    # of them actually runs — not at the `from .capture.compare import ...`
    # statement itself. Wrap the calls, not just the import, to catch it.
    try:
        from .capture.compare import align_series, compute_stats, render_chart

        rates = series_rates(samples)
        # BLE capture lands ~1-2.5 s apart, well inside align_series' 8 s default
        # hold. An API import (`hr-import-whoop`) is 6 s at best and 60/600 s if
        # asked for, which would step off the end of that hold and render as an
        # all-NaN column. Hold for twice the widest median gap so coarse imported
        # sessions still draw; BLE sessions stay on the 8 s default.
        gaps = [r["median_gap_s"] for r in rates.values() if r["median_gap_s"]]
        aligned = align_series(
            samples, max_hold_s=max(8, int(max(gaps) * 2)) if gaps else 8
        )
        # Ground truth is the chest strap. Prefer the live BLE `strap` capture;
        # fall back to an imported `garmin` series, which is the same physical
        # strap by a different transport (and the only one that survives a swim).
        if baseline is None:
            baseline = next(
                (d for d in ("strap", "garmin") if d in aligned.columns), None
            )
        elif baseline not in aligned.columns:
            typer.echo(
                f"Session {session_id} has no device {baseline!r}. Present: "
                f"{', '.join(aligned.columns)}.",
                err=True,
            )
            raise typer.Exit(code=2)
        stats = compute_stats(aligned, rates, baseline=baseline)
    except ImportError:
        typer.echo(
            "pandas/scipy/matplotlib missing. Install with: `uv sync --extra analysis`",
            err=True,
        )
        raise typer.Exit(code=2)

    if stats.get("pairs"):
        from .capture.compare import is_same_sensor

        typer.echo(f"  baseline: {stats['baseline']}")
        for device, p in stats["pairs"].items():
            # Same sensor by another transport is a pipeline check, not a
            # device comparison — say so instead of listing it as agreement.
            if is_same_sensor(device, stats["baseline"]):
                typer.echo(
                    f"  {device} vs {stats['baseline']}: SAME SENSOR "
                    f"(transport check, not a device comparison) "
                    f"mean_abs_diff={p.get('mean_abs_diff')} "
                    f"n_overlap={p['n_overlap']}"
                )
                continue
            typer.echo(
                f"  {device} vs {stats['baseline']}: "
                f"mean_abs_diff={p.get('mean_abs_diff')} "
                f"median_abs_diff={p.get('median_abs_diff')} "
                f"max_abs_diff={p.get('max_abs_diff')} "
                f"pct_within_5={p.get('pct_within_5')} "
                f"pearson_r={p.get('pearson_r')} spearman_rho={p.get('spearman_rho')} "
                f"n_overlap={p['n_overlap']}"
            )
    else:
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
    try:
        render_chart(session_id, aligned, stats, out_path)
    except ImportError:
        typer.echo(
            "pandas/scipy/matplotlib missing. Install with: `uv sync --extra analysis`",
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(f"wrote {out_path}")


@app.command("hr-import-whoop")
def hr_import_whoop(
    date_str: str | None = typer.Option(
        None,
        "--date",
        help="Local date of the workout(s): YYYY-MM-DD | today | yesterday.",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="Merge into an existing hr_session (see hr_sessions.id), using its window.",
    ),
    workout: str | None = typer.Option(
        None, "--workout", help="With --date: import only this Whoop provider_id."
    ),
    step: int = typer.Option(
        6, "--step", help="Bucket seconds (6 | 60 | 600). 6 is the finest Whoop returns."
    ),
) -> None:
    """Import per-sample Whoop workout HR from the internal web-app API.

    The public developer API reports only average/max HR per workout. This pulls
    the underlying series Whoop charts in its own app and lands it in
    `hr_sessions`/`hr_samples`, so `hr-compare` reads it like a BLE capture.
    Needs WHOOP_USERNAME / WHOOP_PASSWORD in .env (account login, not OAuth).

    Two ways in:

    --date  imports each stored Whoop workout that day into its own session.
            Requires `wearable pull --date <date>` to have run first (OAuth).

    --session  fetches the window of an existing BLE capture and merges the
            result into that same session as device `whoop_api`, so hr-compare
            puts it right beside the chest-strap baseline — and fills the
            stretches where the BLE stream dropped out. Needs no OAuth setup.
    """
    if not (settings := load_settings()).whoop_username or not settings.whoop_password:
        typer.echo(
            "WHOOP_USERNAME / WHOOP_PASSWORD missing from .env. The internal HR "
            "endpoint authenticates against your Whoop *account* login — the "
            "OAuth client credentials cannot reach it.",
            err=True,
        )
        raise typer.Exit(code=2)
    if step not in VALID_STEPS:
        typer.echo(
            f"--step must be one of {', '.join(str(s) for s in VALID_STEPS)}; "
            "Whoop quantizes anything else server-side.",
            err=True,
        )
        raise typer.Exit(code=2)
    if (date_str is None) == (session is None):
        typer.echo("Pass exactly one of --date or --session.", err=True)
        raise typer.Exit(code=2)

    configure_logging()
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    client = WhoopInternalClient(
        username=settings.whoop_username, password=settings.whoop_password
    )

    if session is not None:
        _import_into_session(conn, client, session, step)
        return

    day = _parse_date(date_str)
    workouts = load_workouts(conn, device="whoop", day=day.isoformat())
    if workout:
        workouts = [w for w in workouts if w["provider_id"] == workout]
    if not workouts:
        typer.echo(
            f"No Whoop workouts stored for {day.isoformat()}. Run "
            f"`wearable pull --date {day.isoformat()}` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    imported = 0
    for record in workouts:
        provider_id = record["provider_id"]
        sport = record["sport"] or "workout"
        if not record["end_time"]:
            typer.echo(f"  {sport} {provider_id}: skipped (no end_time)", err=True)
            continue
        start = datetime.fromisoformat(record["start_time"])
        end = datetime.fromisoformat(record["end_time"])
        try:
            series = client.fetch_heart_rate(start, end, step=step)
        except Exception as exc:  # network, auth, or shape change — keep going
            typer.echo(f"  {sport} {provider_id}: FAIL {exc}", err=True)
            continue

        # Raw first, always — even a zero-sample response is evidence about
        # whether this undocumented endpoint changed shape on us.
        write_raw_payload(
            conn,
            device="whoop",
            endpoint=series.raw.endpoint,
            date=series.raw.date,
            payload=series.raw.payload,
        )
        if not series.samples:
            typer.echo(f"  {sport} {provider_id}: no samples returned")
            continue

        # Anchor the session at the earlier of workout start and first sample:
        # Whoop's buckets align to step boundaries and can begin just before the
        # workout, and t_offset_ms must stay non-negative for align_series.
        session_start = min(start, series.samples[0][0])
        # `-whoop` suffix keeps API imports in their own id space, so they can
        # never collide with a live BLE session started the same second.
        session_id = f"{session_start.strftime('%Y%m%dT%H%M%SZ')}-whoop"
        if not hr_session_exists(conn, session_id):
            create_hr_session(
                conn,
                session_id=session_id,
                label=sport,
                started_at=session_start.isoformat(),
                devices=["whoop"],
            )
        # PK is (session_id, device, ts_utc) and the timestamps are deterministic,
        # so INSERT OR IGNORE makes re-importing the same workout a no-op.
        insert_hr_samples(
            conn,
            [
                (
                    session_id,
                    "whoop",
                    ts.isoformat(),
                    int((ts - session_start).total_seconds() * 1000),
                    bpm,
                )
                for ts, bpm in series.samples
            ],
        )
        end_hr_session(conn, session_id=session_id, ended_at=end.isoformat())

        _, stored = load_hr_session(conn, session_id)
        rate = series_rates(stored).get("whoop", {})
        # `stored` holds every device in the session, so filter to the series we
        # just imported — otherwise a re-run into a session that already has
        # other devices reports their averages mixed in.
        bpms = [s["bpm"] for s in stored if s["device"] == "whoop"]
        typer.echo(
            f"  {sport} {provider_id} -> session {session_id}: "
            f"n={len(stored)} spacing={rate.get('median_gap_s')}s "
            f"avg={round(sum(bpms) / len(bpms), 1)} max={max(bpms)} "
            f"(API summary avg={record['avg_hr']} max={record['max_hr']})"
        )
        imported += 1

    if not imported:
        raise typer.Exit(code=1)
    typer.echo(
        f"imported {imported} session(s). Chart one with "
        "`uv run wearable hr-compare <session_id>`."
    )


@app.command()
def pull(
    target_date: str = typer.Option(
        "yesterday", "--date", help="ISO date (YYYY-MM-DD) or 'yesterday'."
    ),
    workouts: bool = typer.Option(
        False, "--workouts", help="Also pull recent workout sessions (Whoop + Google Health)."
    ),
    workout_days: int = typer.Option(
        3, "--workout-days", help="How many days back to pull workouts (default 3)."
    ),
    device: list[str] = typer.Option(
        [],
        "--device",
        help="Only pull these devices (repeatable): oura, whoop, google_health. "
        "Default: every device.",
    ),
) -> None:
    """Pull one day across all configured devices (Oura, Whoop, Google Health).

    Exits 0 if all pulled devices succeeded, 2 on partial failure, 1 if all
    failed (or none have credentials). With --device, devices left out are
    neither pulled nor counted.
    """
    known = set(get_args(Device))
    unknown = sorted(set(device) - known)
    if unknown:
        typer.echo(
            f"Unknown --device {', '.join(unknown)}; choose from {', '.join(sorted(known))}",
            err=True,
        )
        raise typer.Exit(code=2)
    day = _parse_date(target_date)
    configure_logging()
    settings = load_settings()

    conn = db.connect(settings.database_path)
    db.migrate(conn)

    # Build client entries once and share them across both the daily-metrics
    # and workouts passes. Whoop rotates its refresh token on every refresh
    # and persists the new one to .env, but `settings` in memory keeps the
    # stale token; rebuilding clients for the workouts pass would refresh an
    # already-consumed token and fail with invalid_grant. Reusing the same
    # client instances keeps the rotated token in memory consistent.
    entries = [e for e in enabled_clients(settings) if not device or e.device in device]

    results = pull_one_day(conn, settings, day, clients=entries)

    for r in results:
        if r.status == "success":
            m = r.metrics
            assert m is not None
            typer.echo(
                f"  {r.device:<14s} OK  sleep={m.total_sleep_minutes} "
                f"readiness={m.readiness_score} sleep_score={m.sleep_score} "
                f"strain={m.strain_or_activity_score}"
            )
        elif r.status == "failed":
            typer.echo(f"  {r.device:<14s} FAIL {r.error}", err=True)
        else:
            typer.echo(f"  {r.device:<14s} SKIP {r.error}")

    s, f, k = summarize(results)
    typer.echo(f"date={day.isoformat()} success={s} failed={f} skipped={k}")
    _sync_inbox(conn, settings, since=default_inbox_since(date.today()))

    if workouts:
        until = day
        since = day - timedelta(days=workout_days - 1)
        wresults = pull_workouts(conn, settings, since, until, clients=entries)
        for r in wresults:
            if r.status == "success":
                typer.echo(f"  {r.device:<14s} OK   {r.count} workouts")
            elif r.status == "failed":
                typer.echo(f"  {r.device:<14s} FAIL {r.error}", err=True)
            else:
                typer.echo(f"  {r.device:<14s} SKIP {r.error}")
        typer.echo(
            f"workouts {since.isoformat()}..{until.isoformat()}: "
            f"{sum(r.count for r in wresults)} total"
        )

    raise typer.Exit(code=exit_code_for(results))


@app.command("log")
def log_cmd(
    score: int | None = typer.Argument(
        None, help="Your subjective readiness, 1-100."
    ),
    fitbit_readiness: int | None = typer.Option(
        None,
        "--fitbit-readiness",
        help="Manually-entered Google Health (Fitbit) readiness, 0-100.",
    ),
    target_date: str = typer.Option(
        "today", "--date", help="ISO date (YYYY-MM-DD), 'today', or 'yesterday'."
    ),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help="Disable interactive prompts (used by tests/scripting).",
    ),
) -> None:
    """Log subjective readiness (1-100) and/or Fitbit readiness (0-100) for a day.

    Never prints existing data — preserves the anchoring discipline of rating
    before viewing wearable numbers.
    """
    day = _parse_date(target_date)
    settings = load_settings()
    conn = db.connect(settings.database_path)
    db.migrate(conn)

    if not no_prompt:
        if score is None:
            raw = typer.prompt(
                "Your readiness 1-100", default="", show_default=False
            ).strip()
            if raw:
                try:
                    score = int(raw)
                except ValueError:
                    typer.echo(f"Invalid subjective score: {raw!r}", err=True)
                    raise typer.Exit(code=2)
        if fitbit_readiness is None:
            raw = typer.prompt(
                "Fitbit readiness 0-100 (Enter to skip)",
                default="",
                show_default=False,
            ).strip()
            if raw:
                try:
                    fitbit_readiness = int(raw)
                except ValueError:
                    typer.echo(f"Invalid Fitbit readiness: {raw!r}", err=True)
                    raise typer.Exit(code=2)

    if score is not None and not (1 <= score <= 100):
        typer.echo(
            f"Subjective readiness must be between 1 and 100 (got {score}).",
            err=True,
        )
        raise typer.Exit(code=2)
    if fitbit_readiness is not None and not (0 <= fitbit_readiness <= 100):
        typer.echo(
            f"Fitbit readiness must be between 0 and 100 (got {fitbit_readiness}).",
            err=True,
        )
        raise typer.Exit(code=2)

    if score is None and fitbit_readiness is None:
        typer.echo("Nothing logged.", err=True)
        raise typer.Exit(code=2)

    parts: list[str] = []
    if score is not None:
        upsert_self_report(conn, day=day, readiness=score)
        parts.append(f"self={score}")
    if fitbit_readiness is not None:
        upsert_manual_readiness(
            conn,
            day=day,
            device="google_health",
            readiness_score=fitbit_readiness,
        )
        parts.append(f"google_health.readiness_score={fitbit_readiness}")

    typer.echo(f"Logged: {', '.join(parts)} for {day.isoformat()}")


@app.command("log-inbox")
def log_inbox() -> None:
    """Write the phone-inbox Google Apps Script, token filled in, and print deploy steps."""
    settings = load_settings()
    if not settings.self_report_token:
        typer.echo("SELF_REPORT_TOKEN is not set in .env (any long random string).", err=True)
        raise typer.Exit(code=2)
    out = settings.database_path.parent / "self_report_inbox.gs"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_inbox_script(settings.self_report_token))
    typer.echo(f"Wrote {out} (contains your token; it's gitignored, don't share it).")
    typer.echo(
        "\n".join(
            [
                "",
                "One-time setup (~5 min):",
                "  1. sheets.new -> name it 'Morning Log'.",
                "  2. Extensions -> Apps Script. Replace Code.gs with the file above. Save.",
                "  3. Deploy -> New deployment -> type 'Web app'.",
                "     Execute as: Me.  Who has access: Anyone.  Deploy, then authorize.",
                "  4. Copy the Web app URL (ends in /exec) into .env as SELF_REPORT_URL=...",
                "  5. Check it: uv run wearable log-sync",
                "Editing the script later? Deploy -> Manage deployments -> edit -> New version,",
                "so the URL stays the same.",
            ]
        )
    )


@app.command("log-sync")
def log_sync(
    all_rows: bool = typer.Option(
        False, "--all", help="Fetch every row, not just the last 60 days."
    ),
) -> None:
    """Pull feeling scores logged on the phone (Morning Log) into self_report.

    `wearable pull` does this automatically; this is the standalone version.
    """
    configure_logging()
    settings = load_settings()
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    since = None if all_rows else default_inbox_since(date.today())
    if not _sync_inbox(conn, settings, since=since):
        raise typer.Exit(code=1)


def _sync_inbox(conn, settings, *, since: date | None) -> bool:
    """Sync the phone inbox, reporting like a device line. Never prints scores
    (anchoring), and a failure here never fails the device pull."""
    if not (settings.self_report_url and settings.self_report_token):
        typer.echo(f"  {'self_report':<14s} SKIP SELF_REPORT_URL/SELF_REPORT_TOKEN not set")
        return False
    try:
        result = sync_inbox(
            conn, url=settings.self_report_url, token=settings.self_report_token, since=since
        )
    except (httpx.HTTPError, InboxError) as exc:
        typer.echo(f"  {'self_report':<14s} FAIL {exc}", err=True)
        return False
    typer.echo(
        f"  {'self_report':<14s} OK   {result.received} on phone, "
        f"{result.written} written, {result.skipped} skipped"
    )
    return True


@app.command()
def viz(
    port: int = typer.Option(8501, "--port", help="Streamlit server port."),
) -> None:
    """Launch the Streamlit visualization UI (requires the `viz` extra)."""
    try:
        from streamlit.web import cli as stcli  # noqa: F401
    except ImportError:
        typer.echo(
            "Streamlit not installed. Install with: `uv sync --extra viz`",
            err=True,
        )
        raise typer.Exit(code=2)
    import sys
    from pathlib import Path

    from streamlit.web import cli as stcli  # re-import inside the success path

    script = Path(__file__).parent / "viz.py"
    sys.argv = [
        "streamlit",
        "run",
        str(script),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
    ]
    sys.exit(stcli.main())


@app.command()
def analyze(
    since: str | None = typer.Option(
        None, "--since", help="ISO date lower bound (YYYY-MM-DD)."
    ),
    until: str | None = typer.Option(
        None, "--until", help="ISO date upper bound (YYYY-MM-DD)."
    ),
    min_n: int = typer.Option(
        14, "--min-n", help="Minimum paired observations to compute rho."
    ),
    csv_path: str | None = typer.Option(None, "--csv", help="Write CSV here."),
    json_path: str | None = typer.Option(None, "--json", help="Write JSON here."),
) -> None:
    """Spearman rank correlation across device pairs (requires the `analysis` extra).

    Pairs always compared: (oura, whoop), (oura, google_health),
    (whoop, google_health). Metrics: readiness_score, sleep_score,
    strain_or_activity_score, hrv_ms, resting_hr, total_sleep_minutes.

    Score fields are never averaged across devices — they use proprietary
    scales. Rank correlation tells you whether two devices agree about which
    days were good and which were bad.
    """
    from .analysis import format_table, run_spearman, write_csv, write_json

    settings = load_settings()
    conn = db.connect(settings.database_path)
    db.migrate(conn)

    since_d = _parse_date(since) if since else None
    until_d = _parse_date(until) if until else None
    rows = run_spearman(conn, since=since_d, until=until_d, min_n=min_n)

    typer.echo(format_table(rows))
    if csv_path:
        write_csv(rows, csv_path)
        typer.echo(f"wrote {csv_path}")
    if json_path:
        write_json(rows, json_path)
        typer.echo(f"wrote {json_path}")


recovery_app = typer.Typer(
    help="Personal recovery model: Whoop replica, custom weights, feeling-tuned "
    "(requires the `analysis` extra)."
)
app.add_typer(recovery_app, name="recovery")


def _recovery_frame():
    """Import the model lazily (pandas/numpy) and load the full feature frame.

    Always the full history: baselines need warm-up days, and the replica is
    the prior for tuning regardless of the window being examined.
    """
    try:
        from . import recovery
    except ImportError:
        typer.echo(
            "pandas/numpy missing. Install with: `uv sync --extra analysis`", err=True
        )
        raise typer.Exit(code=2)
    settings = load_settings()
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    return recovery, recovery.load_frame(conn)


def _date_slice(frame, since: str | None, until: str | None):
    lo = _parse_date(since).isoformat() if since else None
    hi = _parse_date(until).isoformat() if until else None
    return frame.loc[lo:hi]


def _fit_replica_or_exit(recovery, frame):
    try:
        return recovery.fit_replica(frame)
    except ValueError as exc:
        typer.echo(f"Cannot fit the Whoop replica: {exc}", err=True)
        raise typer.Exit(code=1)


def _weights_table(recovery, columns: dict, frame) -> str:
    """Rows per feature: weight (pts per SD) and share, one pair per model."""
    shares = {name: recovery.contribution_shares(m, frame) for name, m in columns.items()}
    header = f"{'feature':<22}" + "".join(f"{name:>21} " for name in columns)
    lines = [header, f"{'':<22}" + "".join(f"{'pts/SD   share':>21} " for _ in columns)]
    for f in recovery.FEATURES:
        cells = "".join(
            f"{m.weights[f]:>+13.2f} {shares[name][f]:>7.0%} " for name, m in columns.items()
        )
        lines.append(f"{recovery.FEATURE_LABELS[f]:<22}{cells}")
    for group in ("hrv", "sleep"):
        cells = "".join(
            f"{'':>13} {recovery.group_share(shares[name], group):>7.0%} " for name in columns
        )
        lines.append(f"{'= ' + group + ' total':<22}{cells}")
    return "\n".join(lines)


@recovery_app.command("explain")
def recovery_explain(
    since: str | None = typer.Option(None, "--since", help="ISO date lower bound."),
    until: str | None = typer.Option(None, "--until", help="ISO date upper bound."),
) -> None:
    """Fit a linear replica of Whoop's recovery and show what drives it.

    Weights are recovery points per 1 SD above your own trailing 30-day normal;
    share is each feature's portion of the total day-to-day influence.
    """
    recovery, frame = _recovery_frame()
    frame = _date_slice(frame, since, until)
    model = _fit_replica_or_exit(recovery, frame)
    rows = recovery.complete_rows(frame, "whoop")
    typer.echo(
        f"Whoop recovery replica: {len(rows)} days "
        f"({rows.index.min():%Y-%m-%d} to {rows.index.max():%Y-%m-%d}), "
        f"intercept {model.intercept:.1f}"
    )
    try:
        acc = recovery.backtest_replica(frame)
        typer.echo(
            f"Walk-forward accuracy on {acc.n} days (each predicted from earlier "
            f"days only): MAE {acc.mae:.1f} pts, R2 {acc.r2:.2f}, "
            f"rank corr {acc.rank_corr:.2f}"
        )
    except ValueError as exc:
        typer.echo(f"(walk-forward accuracy skipped: {exc})")
    typer.echo("")
    typer.echo(_weights_table(recovery, {"whoop replica": model}, rows))


def _parse_scales(values: list[str]) -> dict[str, float]:
    scales: dict[str, float] = {}
    for item in values:
        name, sep, factor = item.partition("=")
        try:
            if not sep:
                raise ValueError
            scales[name.strip()] = float(factor)
        except ValueError:
            typer.echo(f"--scale expects NAME=FACTOR, got {item!r}", err=True)
            raise typer.Exit(code=2)
    return scales


@recovery_app.command("scores")
def recovery_scores(
    days: int = typer.Option(14, "--days", help="How many recent days to show."),
    until: str | None = typer.Option(None, "--until", help="Last day shown (default: latest)."),
    scale: list[str] = typer.Option(
        [],
        "--scale",
        help="Re-weight the replica, e.g. --scale sleep=2 --scale hrv=0.7. "
        "Names: hrv, rhr, resp, skin_temp, spo2, sleep_perf, sleep_dur, sleep_debt, "
        "sleep_consistency, disturbances, prev_strain, or sleep (all five sleep inputs).",
    ),
) -> None:
    """Daily table: Whoop's recovery, the replica, your re-weighted version,
    and the feeling-tuned model once enough feelings are logged."""
    recovery, frame = _recovery_frame()
    frame = _date_slice(frame, None, until)
    replica = _fit_replica_or_exit(recovery, frame)
    columns = {"whoop": frame["whoop"], "replica": replica.predict(frame)}
    scales = _parse_scales(scale)
    if scales:
        try:
            columns["custom"] = replica.scaled(scales).predict(frame)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2)
    try:
        columns["personal"] = recovery.tune_to_feelings(frame, replica).model.predict(frame)
    except ValueError as exc:
        typer.echo(f"(no personal column: {exc})")
    columns["feeling"] = frame["feeling"]

    shown = frame.index[-days:]
    typer.echo(f"{'date':<12}" + "".join(f"{name:>10}" for name in columns))
    for day in shown:
        cells = "".join(
            f"{'-':>10}" if col[day] != col[day] else f"{col[day]:>10.0f}"  # NaN != NaN
            for col in columns.values()
        )
        typer.echo(f"{day:%Y-%m-%d}  {cells}")


@recovery_app.command("tune")
def recovery_tune(
    since: str | None = typer.Option(None, "--since", help="ISO date lower bound."),
    until: str | None = typer.Option(None, "--until", help="ISO date upper bound."),
) -> None:
    """Tune the model to your `wearable log` feelings and test it honestly.

    Starts from the Whoop replica and moves only as far as your feelings
    justify. All accuracies are walk-forward: each day is predicted from
    earlier days only.
    """
    recovery, full = _recovery_frame()
    # The replica is fitted on the full history even when --since/--until
    # narrow the feeling window: Whoop's weights are the prior either way.
    replica = _fit_replica_or_exit(recovery, full)
    frame = _date_slice(full, since, until)
    try:
        result = recovery.tune_to_feelings(frame, replica)
    except ValueError as exc:
        typer.echo(f"Not enough data to tune yet: {exc}.")
        typer.echo("Keep logging every morning with `uv run wearable log` before looking at any device.")
        raise typer.Exit(code=1)

    lam = "inf (feelings don't justify moving off Whoop's weights)" if result.lam == float("inf") else f"{result.lam:g} days"
    typer.echo(f"Tuned on {result.n_days} days of feelings. Whoop prior worth: {lam}")
    typer.echo("")
    rows = recovery.complete_rows(frame, "feeling")
    typer.echo(
        _weights_table(recovery, {"whoop replica": replica, "personal": result.model}, rows)
    )
    typer.echo("")
    typer.echo(f"Predicting your feeling, walk-forward over {result.personal.n} days:")
    typer.echo(f"{'':<34}{'MAE':>7}{'R2':>7}{'rank':>7}")
    for label, acc in (
        ("personal model", result.personal),
        ("Whoop weights, your scale", result.whoop_weights),
        ("Whoop's own score, calibrated", result.whoop_score),
    ):
        typer.echo(f"{label:<34}{acc.mae:>7.1f}{acc.r2:>7.2f}{acc.rank_corr:>7.2f}")


@app.command()
def backfill(
    since: str = typer.Option(..., "--since", help="ISO date (YYYY-MM-DD)."),
    until: str | None = typer.Option(
        None, "--until", help="ISO date (YYYY-MM-DD). Defaults to yesterday."
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        help="Skip days where all configured devices already have rows.",
    ),
) -> None:
    """Backfill every day from --since through --until (or yesterday)."""
    since_d = _parse_date(since)
    until_d = _parse_date(until) if until else None
    configure_logging()
    settings = load_settings()

    conn = db.connect(settings.database_path)
    db.migrate(conn)

    walks = orchestrate_backfill(
        conn, settings, since_d, until=until_d, skip_existing=skip_existing
    )

    total_days = sum(1 for _, r in walks if r)  # skipped (empty) days don't count
    successes = sum(1 for _, results in walks for r in results if r.status == "success")
    failures = sum(1 for _, results in walks for r in results if r.status == "failed")
    typer.echo(
        f"backfill: days_pulled={total_days} successes={successes} failures={failures}"
    )
    if successes == 0 and failures > 0:
        raise typer.Exit(code=1)
    if failures > 0:
        raise typer.Exit(code=2)


@app.command("hr-swim")
def hr_swim(
    date_str: str = typer.Option(
        ..., "--date", help="Local date of the swim: YYYY-MM-DD | today | yesterday."
    ),
    activity: str | None = typer.Option(
        None, "--activity", help="Only this Garmin activityId."
    ),
) -> None:
    """Build an API-only HR session for a swim: Garmin + Whoop + Fitbit, no BLE.

    BLE cannot reach a submerged strap — measured coverage across past swims is
    6–9% of the session — so swims skip `hr-capture` entirely. This anchors the
    session on the Garmin activity (the only source with full underwater
    coverage) and merges the Whoop and Fitbit series into it.

    Partial-failure tolerant: a source that's unconfigured or errors is reported
    and skipped, so one bad credential doesn't cost you the whole swim.
    """
    settings = load_settings()
    configure_logging()
    day = _parse_date(date_str)
    conn = db.connect(settings.database_path)
    db.migrate(conn)

    garmin = GarminClient(
        email=settings.garmin_email,
        password=settings.garmin_password,
        token_dir=settings.garmin_token_dir,
    )
    try:
        session_ids = _garmin_by_date(conn, garmin, day, activity)
    except (ImportError, GarminAuthError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    whoop = None
    if settings.whoop_username and settings.whoop_password:
        whoop = WhoopInternalClient(
            username=settings.whoop_username, password=settings.whoop_password
        )
    fitbit = None
    if settings.google_client_id and settings.google_refresh_token:
        fitbit = GoogleHealthClient(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret or "",
            refresh_token=settings.google_refresh_token,
            env_path=settings.env_path,
            local_timezone=settings.local_timezone,
        )

    for session_id in session_ids:
        session, _ = load_hr_session(conn, session_id)
        start = datetime.fromisoformat(session["started_at"])
        end = datetime.fromisoformat(session["ended_at"] or session["started_at"])
        typer.echo(f"session {session_id} ({session['label'] or 'unlabeled'}):")

        if whoop is None:
            typer.echo("  whoop: SKIP (WHOOP_USERNAME / WHOOP_PASSWORD not set)")
        else:
            _merge_api_source(
                conn,
                session_id=session_id,
                label="whoop",
                device="whoop_api",
                raw_device="whoop",
                fetch=lambda: whoop.fetch_heart_rate(start, end),
            )
        if fitbit is None:
            typer.echo("  fitbit: SKIP (run `wearable auth google` first)")
        else:
            _merge_api_source(
                conn,
                session_id=session_id,
                label="fitbit",
                device="fitbit_api",
                raw_device="google_health",
                fetch=lambda: fitbit.fetch_heart_rate(start, end),
            )
        _print_session_rates(conn, session_id)
        typer.echo(
            f"  chart: `uv run wearable hr-compare {session_id}` "
            "(baseline falls back to garmin)"
        )


def _merge_api_source(
    conn, *, session_id: str, label: str, device: str, raw_device: str, fetch
) -> None:
    """Fetch one API source into a session, tolerating that source failing.

    Raw is archived even on an empty result — an empty window is itself
    evidence (e.g. Fitbit records nothing while swimming).
    """
    try:
        series = fetch()
    except Exception as exc:
        typer.echo(f"  {label}: FAIL {type(exc).__name__}: {exc}", err=True)
        return
    write_raw_payload(
        conn,
        device=raw_device,
        endpoint=series.raw.endpoint,
        date=series.raw.date,
        payload=series.raw.payload,
    )
    added = _merge_samples_into_session(
        conn, session_id=session_id, device=device, samples=series.samples
    )
    if added:
        typer.echo(f"  {label}: +{added} samples")
    else:
        typer.echo(f"  {label}: no samples in this window")


@app.command("hr-import-fitbit")
def hr_import_fitbit(
    date_str: str | None = typer.Option(
        None, "--date", help="Local date of the workout(s): YYYY-MM-DD | today | yesterday."
    ),
    session: str | None = typer.Option(
        None, "--session", help="Merge into an existing hr_session, using its window."
    ),
    workout: str | None = typer.Option(
        None, "--workout", help="With --date: import only this google_health provider_id."
    ),
) -> None:
    """Import per-sample Fitbit HR from Google Health's intraday endpoint.

    Fitbit's cadence is roughly one sample per 2 s — better than the BLE
    stream, still short of Garmin's true 1 Hz. Delivered spacing is reported.

    Unlike the BLE capture (which only works while the phone's "Share Heart
    Rate" toggle is live and the watch is in range), this reads what Fitbit
    already stored. Note the Fitbit Air records no HR at all while swimming,
    so a swim window comes back near-empty however it's fetched.

    --date     each stored google_health workout that day -> its own session.
    --session  merge into an existing BLE capture as device `fitbit_api`.
    """
    settings = load_settings()
    if not settings.google_client_id or not settings.google_refresh_token:
        typer.echo(
            "GOOGLE_HEALTH_CLIENT_ID / GOOGLE_HEALTH_REFRESH_TOKEN missing from "
            ".env. Run `wearable auth google` first.",
            err=True,
        )
        raise typer.Exit(code=2)
    if (date_str is None) == (session is None):
        typer.echo("Pass exactly one of --date or --session.", err=True)
        raise typer.Exit(code=2)

    configure_logging()
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    client = GoogleHealthClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret or "",
        refresh_token=settings.google_refresh_token,
        env_path=settings.env_path,
        local_timezone=settings.local_timezone,
    )

    if session is not None:
        _fitbit_into_session(conn, client, session)
        return

    day = _parse_date(date_str)
    workouts = load_workouts(conn, device="google_health", day=day.isoformat())
    if workout:
        workouts = [w for w in workouts if w["provider_id"] == workout]
    if not workouts:
        typer.echo(
            f"No Fitbit workouts stored for {day.isoformat()}. Run "
            f"`wearable pull --date {day.isoformat()}` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    imported = 0
    for record in workouts:
        sport = record["sport"] or "workout"
        if not record["end_time"]:
            typer.echo(f"  {sport}: skipped (no end_time)", err=True)
            continue
        start = datetime.fromisoformat(record["start_time"])
        end = datetime.fromisoformat(record["end_time"])
        series = client.fetch_heart_rate(start, end)
        write_raw_payload(
            conn,
            device="google_health",
            endpoint=series.raw.endpoint,
            date=series.raw.date,
            payload=series.raw.payload,
        )
        if not series.samples:
            typer.echo(f"  {sport} {record['provider_id']}: no samples returned")
            continue

        anchor = min(start, series.samples[0][0])
        session_id = f"{anchor.strftime('%Y%m%dT%H%M%SZ')}-fitbit"
        if not hr_session_exists(conn, session_id):
            create_hr_session(
                conn,
                session_id=session_id,
                label=sport,
                started_at=anchor.isoformat(),
                devices=["fitbit_api"],
            )
        added = _merge_samples_into_session(
            conn, session_id=session_id, device="fitbit_api", samples=series.samples
        )
        end_hr_session(conn, session_id=session_id, ended_at=end.isoformat())
        _, stored = load_hr_session(conn, session_id)
        rate = series_rates(stored).get("fitbit_api", {})
        bpms = [s["bpm"] for s in stored if s["device"] == "fitbit_api"]
        typer.echo(
            f"  {sport} {record['provider_id']} -> session {session_id}: "
            f"n={added} spacing={rate.get('median_gap_s')}s "
            f"avg={round(sum(bpms) / len(bpms), 1)} max={max(bpms)}"
        )
        imported += 1

    if not imported:
        raise typer.Exit(code=1)
    typer.echo(
        f"imported {imported} session(s). Chart one with "
        "`uv run wearable hr-compare <session_id>`."
    )


def _fitbit_into_session(conn, client, session_id: str) -> None:
    try:
        session, _ = load_hr_session(conn, session_id)
    except KeyError:
        typer.echo(f"No session {session_id!r}.", err=True)
        raise typer.Exit(code=1)
    if not session["ended_at"]:
        typer.echo(
            f"Session {session_id} has no ended_at — can't bound the fetch.", err=True
        )
        raise typer.Exit(code=1)

    start = datetime.fromisoformat(session["started_at"])
    end = datetime.fromisoformat(session["ended_at"])
    series = client.fetch_heart_rate(start, end)
    write_raw_payload(
        conn,
        device="google_health",
        endpoint=series.raw.endpoint,
        date=series.raw.date,
        payload=series.raw.payload,
    )

    added = _merge_samples_into_session(
        conn, session_id=session_id, device="fitbit_api", samples=series.samples
    )
    if not added:
        typer.echo(
            f"No Fitbit samples returned for {session_id} "
            f"({start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC).",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"session {session_id} ({session['label'] or 'unlabeled'}):")
    _print_session_rates(conn, session_id)
    typer.echo(
        f"added {added} fitbit_api samples. Chart it with "
        f"`uv run wearable hr-compare {session_id}`."
    )


@app.command("hr-import-garmin")
def hr_import_garmin(
    date_str: str | None = typer.Option(
        None, "--date", help="Local date of the activity: YYYY-MM-DD | today | yesterday."
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="Merge into an existing hr_session, matching the Garmin activity by overlap.",
    ),
    activity: str | None = typer.Option(
        None, "--activity", help="With --date: import only this Garmin activityId."
    ),
) -> None:
    """Import per-sample Garmin activity HR from Garmin Connect.

    Garmin returns the samples the device actually recorded — true 1 s under
    "Every Second" recording — so unlike Whoop (6 s floor) there's no
    server-side bucketing. The delivered spacing is reported, not assumed.

    --date     imports each Garmin activity that day into its own session.
    --session  finds the Garmin activity overlapping an existing BLE capture's
               window and merges it in as device `garmin`.

    Needs the `garmin` extra (`uv sync --extra garmin`). First run uses
    GARMIN_EMAIL / GARMIN_PASSWORD from .env and prompts for MFA; after that
    the cached SSO tokens (~1 year) are enough.
    """
    settings = load_settings()
    if (date_str is None) == (session is None):
        typer.echo("Pass exactly one of --date or --session.", err=True)
        raise typer.Exit(code=2)

    configure_logging()
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    client = GarminClient(
        email=settings.garmin_email,
        password=settings.garmin_password,
        token_dir=settings.garmin_token_dir,
    )

    try:
        if session is not None:
            _garmin_into_session(conn, client, session)
        else:
            created = _garmin_by_date(conn, client, _parse_date(date_str), activity)
            typer.echo(
                f"imported {len(created)} session(s). Chart one with "
                "`uv run wearable hr-compare <session_id>`."
            )
    except ImportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    except GarminAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)


def _garmin_into_session(conn, client: GarminClient, session_id: str) -> None:
    try:
        session, _ = load_hr_session(conn, session_id)
    except KeyError:
        typer.echo(f"No session {session_id!r}.", err=True)
        raise typer.Exit(code=1)
    if not session["ended_at"]:
        typer.echo(
            f"Session {session_id} has no ended_at — can't match an activity.", err=True
        )
        raise typer.Exit(code=1)

    start = datetime.fromisoformat(session["started_at"])
    end = datetime.fromisoformat(session["ended_at"])
    # Garmin lists by *local* start date; widen by a day either side so a
    # session spanning local midnight (or a UTC/local offset) still matches.
    activities = client.list_activities(
        start.date() - timedelta(days=1), end.date() + timedelta(days=1)
    )
    match = _pick_activity_for_window(activities, start, end)
    if match is None:
        typer.echo(
            f"No Garmin activity overlaps {session_id} "
            f"({start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC).",
            err=True,
        )
        raise typer.Exit(code=1)

    series = client.fetch_activity_hr(
        match["activityId"], duration_s=match.get("duration")
    )
    write_raw_payload(
        conn,
        device="garmin",
        endpoint=series.raw.endpoint,
        date=start.date().isoformat(),
        payload=series.raw.payload,
    )
    name = match.get("activityName") or "activity"
    if not series.samples:
        typer.echo(
            f"Garmin activity {match['activityId']} ({name}) has no heart-rate "
            "channel — it was likely entered by hand rather than recorded.",
            err=True,
        )
        raise typer.Exit(code=1)

    added = _merge_samples_into_session(
        conn, session_id=session_id, device="garmin", samples=series.samples
    )
    if not added:
        typer.echo(
            f"Garmin activity {match['activityId']} has samples, but none fall "
            f"inside {session_id}'s window.",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"session {session_id} ({session['label'] or 'unlabeled'}):")
    _print_session_rates(conn, session_id)
    typer.echo(
        f"added {added} garmin samples from activity {match['activityId']} "
        f"({name}). Chart it with `uv run wearable hr-compare {session_id}`."
    )


def _garmin_by_date(
    conn, client: GarminClient, day: date, activity_id: str | None
) -> list[str]:
    activities = client.list_activities(day)
    if activity_id:
        activities = [
            a for a in activities if str(a.get("activityId")) == str(activity_id)
        ]
    if not activities:
        typer.echo(f"No Garmin activities on {day.isoformat()}.", err=True)
        raise typer.Exit(code=1)

    session_ids: list[str] = []
    for match in activities:
        name = match.get("activityName") or "activity"
        start = activity_start_utc(match)
        if start is None:
            typer.echo(f"  {name}: skipped (unparseable startTimeGMT)", err=True)
            continue
        series = client.fetch_activity_hr(
            match["activityId"], duration_s=match.get("duration")
        )
        write_raw_payload(
            conn,
            device="garmin",
            endpoint=series.raw.endpoint,
            date=day.isoformat(),
            payload=series.raw.payload,
        )
        if not series.samples:
            typer.echo(f"  {name}: no heart-rate channel (hand-entered activity?)")
            continue

        anchor = min(start, series.samples[0][0])
        session_id = f"{anchor.strftime('%Y%m%dT%H%M%SZ')}-garmin"
        if not hr_session_exists(conn, session_id):
            create_hr_session(
                conn,
                session_id=session_id,
                label=name,
                started_at=anchor.isoformat(),
                devices=["garmin"],
            )
        added = _merge_samples_into_session(
            conn, session_id=session_id, device="garmin", samples=series.samples
        )
        end_hr_session(
            conn, session_id=session_id, ended_at=series.samples[-1][0].isoformat()
        )
        _, stored = load_hr_session(conn, session_id)
        rate = series_rates(stored).get("garmin", {})
        bpms = [s["bpm"] for s in stored if s["device"] == "garmin"]
        typer.echo(
            f"  {name} {match['activityId']} -> session {session_id}: "
            f"n={added} spacing={rate.get('median_gap_s')}s "
            f"avg={round(sum(bpms) / len(bpms), 1)} max={max(bpms)}"
        )
        session_ids.append(session_id)

    if not session_ids:
        raise typer.Exit(code=1)
    return session_ids


def _pick_activity_for_window(
    activities: list, start: datetime, end: datetime
) -> dict | None:
    """The activity overlapping [start, end] the most, if any actually do."""
    best, best_overlap = None, 0.0
    for act in activities:
        act_start = activity_start_utc(act)
        if act_start is None:
            continue
        act_end = act_start + timedelta(seconds=float(act.get("duration") or 0))
        overlap = (min(end, act_end) - max(start, act_start)).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = act, overlap
    return best


def _import_into_session(
    conn, client: WhoopInternalClient, session_id: str, step: int
) -> None:
    """Merge Whoop's stored HR for an existing session's window into that session.

    Lands under device key `whoop_api` so it sits *alongside* — never on top of —
    the `whoop` series the BLE capture recorded live. That's the point: the two
    are independent measurements of the same wrist, and the API series covers the
    stretches where BLE dropped out (a swimmer underwater, say).
    """
    try:
        session, _ = load_hr_session(conn, session_id)
    except KeyError:
        typer.echo(f"No session {session_id!r}.", err=True)
        raise typer.Exit(code=1)
    if not session["ended_at"]:
        typer.echo(
            f"Session {session_id} has no ended_at — can't bound the fetch.", err=True
        )
        raise typer.Exit(code=1)

    start = datetime.fromisoformat(session["started_at"])
    end = datetime.fromisoformat(session["ended_at"])
    try:
        series = client.fetch_heart_rate(start, end, step=step)
    except Exception as exc:
        typer.echo(f"FAIL {exc}", err=True)
        raise typer.Exit(code=1)

    write_raw_payload(
        conn,
        device="whoop",
        endpoint=series.raw.endpoint,
        date=series.raw.date,
        payload=series.raw.payload,
    )

    added = _merge_samples_into_session(
        conn, session_id=session_id, device="whoop_api", samples=series.samples
    )
    if not added:
        typer.echo(
            f"No samples returned for {session_id} "
            f"({start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC)."
        )
        raise typer.Exit(code=1)

    typer.echo(f"session {session_id} ({session['label'] or 'unlabeled'}):")
    _print_session_rates(conn, session_id)
    typer.echo(
        f"added {added} whoop_api samples. Chart it with "
        f"`uv run wearable hr-compare {session_id}`."
    )


def _merge_samples_into_session(
    conn, *, session_id: str, device: str, samples: list
) -> int:
    """Insert samples into an existing session, anchored at its ``started_at``.

    That anchor is already fixed by whatever created the session, so samples
    predating it are dropped rather than emitting a negative t_offset_ms.
    `hr_samples`' PK is (session_id, device, ts_utc) and source timestamps are
    deterministic, so INSERT OR IGNORE makes a re-run a no-op.
    """
    session, _ = load_hr_session(conn, session_id)
    start = datetime.fromisoformat(session["started_at"])
    rows = [
        (
            session_id,
            device,
            ts.isoformat(),
            int((ts - start).total_seconds() * 1000),
            bpm,
        )
        for ts, bpm in samples
        if ts >= start
    ]
    if not rows:
        return 0
    insert_hr_samples(conn, rows)
    add_hr_session_device(conn, session_id=session_id, device=device)
    return len(rows)


def _print_session_rates(conn, session_id: str) -> None:
    _, stored = load_hr_session(conn, session_id)
    for device, rate in series_rates(stored).items():
        typer.echo(
            f"  {device}: n={rate['n']} spacing={rate['median_gap_s']}s "
            f"rate={rate['effective_hz']}Hz"
        )


def _parse_date(value: str) -> date:
    if value == "today":
        return date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(value)


if __name__ == "__main__":
    app()
