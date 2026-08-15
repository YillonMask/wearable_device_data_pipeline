from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import typer

from . import db
from .auth.google_flow import run_interactive_flow as run_google_flow
from .auth.whoop_flow import run_interactive_flow as run_whoop_flow
from .capture.ble_hr import capture_session, scan_hr_peripherals
# series_rates is stdlib-only (pandas/scipy live behind lazy imports in the
# other compare helpers), so it is safe to import without the `analysis` extra.
from .capture.compare import series_rates
from .clients._oauth import update_env_var
from .clients.whoop_internal import VALID_STEPS, WhoopInternalClient
from .config import load_settings
from .logging_setup import configure_logging
from .orchestrator import (
    backfill as orchestrate_backfill,
    enabled_clients,
    exit_code_for,
    pull_one_day,
    pull_workouts,
    summarize,
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
    """Run the OAuth flow for a device (whoop | google).

    Oura uses a personal access token — set OURA_PERSONAL_ACCESS_TOKEN in
    .env directly; no auth command needed.
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
        # When a chest strap is in the session, treat it as the ground-truth
        # baseline: each wearable's agreement is reported against the strap.
        baseline = "strap" if "strap" in aligned.columns else None
        stats = compute_stats(aligned, rates, baseline=baseline)
    except ImportError:
        typer.echo(
            "pandas/scipy/matplotlib missing. Install with: `uv sync --extra analysis`",
            err=True,
        )
        raise typer.Exit(code=2)

    if stats.get("pairs"):
        typer.echo(f"  baseline: {stats['baseline']}")
        for device, p in stats["pairs"].items():
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
        bpms = [s["bpm"] for s in stored]
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
) -> None:
    """Pull one day across all configured devices (Oura, Whoop, Google Health).

    Exits 0 if all configured devices succeeded, 2 on partial failure, 1 if
    all configured devices failed (or none have credentials).
    """
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
    entries = enabled_clients(settings)

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

    # This session's anchor is already fixed by the BLE capture, so drop any
    # bucket predating it rather than emitting a negative t_offset_ms.
    rows = [
        (
            session_id,
            "whoop_api",
            ts.isoformat(),
            int((ts - start).total_seconds() * 1000),
            bpm,
        )
        for ts, bpm in series.samples
        if ts >= start
    ]
    if not rows:
        typer.echo(
            f"No samples returned for {session_id} "
            f"({start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC)."
        )
        raise typer.Exit(code=1)

    insert_hr_samples(conn, rows)
    add_hr_session_device(conn, session_id=session_id, device="whoop_api")

    _, stored = load_hr_session(conn, session_id)
    typer.echo(f"session {session_id} ({session['label'] or 'unlabeled'}):")
    for device, rate in series_rates(stored).items():
        typer.echo(
            f"  {device}: n={rate['n']} spacing={rate['median_gap_s']}s "
            f"rate={rate['effective_hz']}Hz"
        )
    typer.echo(
        f"added {len(rows)} whoop_api samples. Chart it with "
        f"`uv run wearable hr-compare {session_id}`."
    )


def _parse_date(value: str) -> date:
    if value == "today":
        return date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(value)


if __name__ == "__main__":
    app()
