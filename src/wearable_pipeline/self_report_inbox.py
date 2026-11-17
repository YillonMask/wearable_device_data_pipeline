"""Pull feeling scores from the Morning Log inbox into ``self_report``.

The Android app (``android/``) posts each morning's 1-100 score to a Google
Apps Script web app bound to a Google Sheet (``apps_script/self_report_inbox.gs``).
This module reads that sheet back through the script's ``doGet`` and upserts
the rows. The phone's ``logged_at`` is kept, and for a date logged in two
places (phone and ``wearable log``) the later ``logged_at`` wins.

Raw-first, like every device client: the response is archived in
``raw_payloads`` (device ``self_report``) before any row is written.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .storage import upsert_self_report, write_raw_payload

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "apps_script" / "self_report_inbox.gs"
TOKEN_PLACEHOLDER = "__SELF_REPORT_TOKEN__"
# The sheet holds every score ever logged; re-fetching all of it twice a day
# would bloat raw_payloads. A rolling window covers any realistic gap
# (laptop off for weeks); `log-sync --all` fetches everything.
DEFAULT_LOOKBACK_DAYS = 60
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncResult:
    received: int
    written: int
    skipped: int  # malformed, already stored, or older than what self_report has


def render_script(token: str) -> str:
    return TEMPLATE_PATH.read_text().replace(TOKEN_PLACEHOLDER, token)


def sync(
    conn: sqlite3.Connection,
    *,
    url: str,
    token: str,
    since: date | None,
    client: httpx.Client | None = None,
) -> SyncResult:
    params = {"token": token}
    if since is not None:
        params["since"] = since.isoformat()
    http = client or httpx.Client(timeout=30)
    try:
        # Apps Script answers with a 302 to googleusercontent.com.
        resp = http.get(url, params=params, follow_redirects=True)
    finally:
        if client is None:
            http.close()
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError as exc:
        # A non-JSON page usually means the deployment isn't "Anyone" access
        # (Google serves a sign-in page) or the URL is not the /exec URL.
        raise InboxError(
            "inbox did not return JSON; check the web app URL ends in /exec "
            "and the deployment's access is 'Anyone'"
        ) from exc
    if not body.get("ok"):
        raise InboxError(f"inbox refused the request: {body.get('error', body)}")

    write_raw_payload(
        conn, device="self_report", endpoint="apps_script/doGet", date=None, payload=body
    )

    rows = body.get("rows", [])
    written = skipped = 0
    for row in rows:
        parsed = _parse_row(row)
        if parsed is None:
            skipped += 1
            continue
        day, readiness, logged_at = parsed
        if upsert_self_report(conn, day=day, readiness=readiness, logged_at=logged_at):
            written += 1
        else:
            skipped += 1
    return SyncResult(received=len(rows), written=written, skipped=skipped)


def default_since(today: date) -> date:
    return today - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def _parse_row(row: dict) -> tuple[date, int, datetime] | None:
    try:
        raw_date = str(row["date"])
        readiness = row["readiness"]
        if not _DATE.match(raw_date) or not isinstance(readiness, (int, float)):
            return None
        if readiness != int(readiness) or not 1 <= readiness <= 100:
            return None
        logged_at = datetime.fromisoformat(str(row["logged_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if logged_at.tzinfo is None:
        return None
    return date.fromisoformat(raw_date), int(readiness), logged_at.astimezone(timezone.utc)
