"""Whoop internal (web-app) API client — per-sample workout heart rate.

Whoop's *public* developer API (``/developer/v2/...``, see ``whoop.py``) exposes
only aggregate workout HR: ``average_heart_rate``, ``max_heart_rate`` and
``zone_durations``. There is no time-series resource anywhere in the v2 endpoint
list, and the member CSV data export is aggregate-only too. The sample-level
series the Whoop app charts comes from an **internal** endpoint the web app
calls for its own graphs:

    GET /metrics-service/v1/metrics/user/{user_id}
        ?name=heart_rate&start=<iso>&end=<iso>&step=<seconds>&apiVersion=7
    -> {"values": [{"time": <unix_ms>, "data": <bpm>}, ...]}

``step`` is the server-side bucket width in seconds; the web app only ever asks
for 6, 60 or 600, and 6 s is the finest retrospective resolution Whoop returns.
True 1 Hz Whoop HR exists **only** on the live BLE broadcast
(``capture/ble_hr.py``) — it cannot be recovered after the fact, so callers
should report the spacing actually delivered rather than the step requested.

This endpoint is unofficial, undocumented and can change without notice. It
authenticates against the member's **account** credentials — the OAuth developer
token used by ``whoop.py`` is rejected — so it lives in its own module on
purpose: a break here must never take down the daily metrics pull.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import RawPayload

BASE_URL = "https://api.prod.whoop.com"
SIGN_IN_ENDPOINT = "/auth-service/v2/whoop/sign-in"
USER_ENDPOINT = "/auth-service/v2/user"
METRICS_ENDPOINT = "/metrics-service/v1/metrics/user"

# Bucket widths the web app requests. 6 s is the finest Whoop honors; other
# values are quantized server-side, so never trust the requested step — measure
# the delivered spacing from the returned timestamps instead.
VALID_STEPS = (6, 60, 600)
FINEST_STEP_S = 6
_API_VERSION = "7"


class WhoopAuthError(RuntimeError):
    """Sign-in with the member's account credentials failed."""


@dataclass(frozen=True)
class HeartRateSeries:
    """Per-sample HR for one time window, plus the archived raw response."""

    samples: list[tuple[datetime, int]]
    raw: RawPayload


class WhoopInternalClient:
    device = "whoop"

    def __init__(
        self,
        *,
        username: str,
        password: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._injected_client = http_client
        self._access_token: str | None = None
        self._user_id: str | None = None

    def fetch_heart_rate(
        self, start: datetime, end: datetime, step: int = FINEST_STEP_S
    ) -> HeartRateSeries:
        """Fetch the HR series covering ``[start, end]`` (both tz-aware UTC)."""
        params = {
            "name": "heart_rate",
            "start": _iso_ms(start),
            "end": _iso_ms(end),
            "step": step,
            "apiVersion": _API_VERSION,
        }

        client, owned = self._open_client()
        try:
            if self._access_token is None:
                self._sign_in(client)
            path = f"{METRICS_ENDPOINT}/{self._user_id}"
            resp = client.get(path, params=params, headers=self._auth_header())
            if resp.status_code in (401, 403):
                # Access token expired — the internal API has no documented
                # refresh grant, so re-run the full sign-in and retry once.
                self._sign_in(client)
                path = f"{METRICS_ENDPOINT}/{self._user_id}"
                resp = client.get(path, params=params, headers=self._auth_header())
            resp.raise_for_status()
            body = resp.json()
        finally:
            if owned:
                client.close()

        raw = RawPayload(
            endpoint=f"{METRICS_ENDPOINT}/heart_rate",
            date=start.date().isoformat(),
            payload=body,
        )
        return HeartRateSeries(samples=_parse_values(body), raw=raw)

    def _sign_in(self, client: httpx.Client) -> None:
        resp = client.post(
            SIGN_IN_ENDPOINT,
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            # Never echo the request body — it carries the account password.
            raise WhoopAuthError(
                f"Whoop sign-in failed ({resp.status_code}). Check "
                "WHOOP_USERNAME / WHOOP_PASSWORD in .env. If you use Google/Apple "
                "sign-in for Whoop, set an account password in the Whoop app first."
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise WhoopAuthError("Whoop sign-in returned no access_token.")
        self._access_token = token
        self._user_id = _user_id_from(body) or self._fetch_user_id(client)

    def _fetch_user_id(self, client: httpx.Client) -> str:
        resp = client.get(USER_ENDPOINT, headers=self._auth_header())
        resp.raise_for_status()
        user_id = _user_id_from(resp.json())
        if not user_id:
            raise WhoopAuthError("Could not resolve Whoop user id after sign-in.")
        return user_id

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _open_client(self) -> tuple[httpx.Client, bool]:
        if self._injected_client is not None:
            return self._injected_client, False
        return httpx.Client(base_url=BASE_URL, timeout=30.0), True


def _user_id_from(body: dict[str, Any]) -> str | None:
    """User id lives at ``user.id`` on both sign-in and profile responses."""
    user = body.get("user")
    if isinstance(user, dict) and user.get("id") is not None:
        return str(user["id"])
    if body.get("id") is not None:
        return str(body["id"])
    return None


def _parse_values(body: dict[str, Any]) -> list[tuple[datetime, int]]:
    """``values[] -> (utc_datetime, bpm)``, dropping gap buckets.

    Whoop emits a bucket with a null/absent ``data`` whenever the strap had no
    reading (off wrist, out of contact). Those are real gaps — drop them rather
    than zero-filling, so the missing stretch stays visible downstream.
    """
    samples: list[tuple[datetime, int]] = []
    for value in body.get("values") or []:
        if not isinstance(value, dict):
            continue
        ts_ms, bpm = value.get("time"), value.get("data")
        if ts_ms is None or not isinstance(bpm, (int, float)):
            continue
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        samples.append((ts, round(bpm)))
    samples.sort(key=lambda pair: pair[0])
    return samples


def _iso_ms(dt: datetime) -> str:
    """The web app sends millisecond-precision Zulu timestamps."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
