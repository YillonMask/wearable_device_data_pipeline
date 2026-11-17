from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest
import respx

from wearable_pipeline.self_report_inbox import InboxError, sync
from wearable_pipeline.storage import upsert_self_report

EXEC_URL = "https://script.google.com/macros/s/abc/exec"
ECHO_URL = "https://script.googleusercontent.com/macros/echo?user_content_key=xyz"


def _inbox(body: dict) -> respx.Route:
    """Mimic Apps Script: /exec answers 302, the echo URL serves the JSON."""
    respx.get(ECHO_URL).mock(return_value=httpx.Response(200, json=body))
    return respx.get(EXEC_URL).mock(
        return_value=httpx.Response(302, headers={"Location": ECHO_URL})
    )


@respx.mock
def test_sync_follows_redirect_archives_raw_and_keeps_phone_timestamps(migrated_db) -> None:
    route = _inbox(
        {
            "ok": True,
            "rows": [
                {"date": "2026-11-07", "readiness": 64, "logged_at": "2026-11-07T15:02:11Z"},
                {"date": "2026-11-08", "readiness": 0, "logged_at": "2026-11-08T15:00:00Z"},
                {"date": "11/08/2026", "readiness": 50, "logged_at": "2026-11-08T15:00:00Z"},
                {"date": "2026-11-09", "readiness": 71, "logged_at": "not a time"},
            ],
        }
    )

    result = sync(migrated_db, url=EXEC_URL, token="secret", since=date(2026, 9, 8))

    params = route.calls.last.request.url.params
    assert params["token"] == "secret" and params["since"] == "2026-09-08"
    assert (result.received, result.written, result.skipped) == (4, 1, 3)
    rows = migrated_db.execute("SELECT * FROM self_report").fetchall()
    assert [(r["date"], r["readiness"]) for r in rows] == [("2026-11-07", 64)]
    assert datetime.fromisoformat(rows[0]["logged_at"]) == datetime(
        2026, 11, 7, 15, 2, 11, tzinfo=timezone.utc
    )
    raw = migrated_db.execute("SELECT device, endpoint FROM raw_payloads").fetchall()
    assert [(r["device"], r["endpoint"]) for r in raw] == [("self_report", "apps_script/doGet")]


@respx.mock
def test_older_phone_entry_never_overwrites_a_newer_laptop_log(migrated_db) -> None:
    upsert_self_report(
        migrated_db,
        day=date(2026, 11, 7),
        readiness=40,
        logged_at=datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc),
    )
    _inbox(
        {
            "ok": True,
            "rows": [
                {"date": "2026-11-07", "readiness": 64, "logged_at": "2026-11-07T15:02:11Z"},
                {"date": "2026-11-08", "readiness": 70, "logged_at": "2026-11-08T15:00:00Z"},
            ],
        }
    )

    result = sync(migrated_db, url=EXEC_URL, token="secret", since=None)

    assert (result.written, result.skipped) == (1, 1)
    rows = migrated_db.execute("SELECT date, readiness FROM self_report ORDER BY date").fetchall()
    assert [(r["date"], r["readiness"]) for r in rows] == [("2026-11-07", 40), ("2026-11-08", 70)]

    # The morning job syncs twice; the second pass over the same rows is a no-op.
    again = sync(migrated_db, url=EXEC_URL, token="secret", since=None)
    assert (again.written, again.skipped) == (0, 2)


@respx.mock
def test_refused_or_non_json_inbox_writes_nothing(migrated_db) -> None:
    _inbox({"ok": False, "error": "unauthorized"})
    with pytest.raises(InboxError, match="unauthorized"):
        sync(migrated_db, url=EXEC_URL, token="wrong", since=None)

    # A deployment without "Anyone" access serves Google's HTML sign-in page.
    respx.get(ECHO_URL).mock(return_value=httpx.Response(200, text="<html>Sign in</html>"))
    with pytest.raises(InboxError, match="Anyone"):
        sync(migrated_db, url=EXEC_URL, token="secret", since=None)

    for table in ("self_report", "raw_payloads"):
        assert migrated_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
