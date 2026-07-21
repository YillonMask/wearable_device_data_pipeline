from __future__ import annotations

import asyncio
from datetime import date, timedelta

import typer

from . import db
from .auth.google_flow import run_interactive_flow as run_google_flow
from .auth.whoop_flow import run_interactive_flow as run_whoop_flow
from .clients._oauth import update_env_var
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
from .capture.ble_hr import scan_hr_peripherals
from .storage import upsert_manual_readiness, upsert_self_report

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
            "bleak not installed. Install with: `uv sync --extra ble`"
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


def _parse_date(value: str) -> date:
    if value == "today":
        return date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(value)


if __name__ == "__main__":
    app()
