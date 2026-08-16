"""Garmin Connect client — per-sample workout heart rate.

Garmin is the one source here that can hand back **true per-second** HR. Where
Whoop's retrospective floor is a 6 s bucket (see ``whoop_internal.py``) and
Google Health's is ~5 s, Garmin returns the samples the device actually
recorded: 1 s under "Every Second" recording, variable under "Smart Recording".
So the delivered spacing is a property of the device's recording setting — the
caller must measure it from the samples, never assume 1 Hz.

    GET /activity-service/activity/{id}/details?maxChartSize=N&maxPolylineSize=0
    -> {"metricDescriptors": [{"key": "directHeartRate", "metricsIndex": 5}, ...],
        "activityDetailMetrics": [{"metrics": [<positional values>]}, ...]}

Two things bite here:

* **Values are positional.** ``activityDetailMetrics[].metrics`` is a bare
  array; ``metricDescriptors[].metricsIndex`` maps array position to metric
  name, and the layout varies by device and activity type. Never hardcode an
  index — resolve it from the descriptors every time.
* **``maxChartSize`` silently downsamples.** Above the cap Garmin thins the
  series instead of erroring, so a long activity fetched at the 2000 default
  comes back quietly decimated. We size it from the activity duration.

Auth is Garmin Connect SSO via the ``garminconnect`` package, which as of
0.3.10 does its own auth (``curl_cffi`` TLS impersonation + rotating user
agents) and no longer depends on the deprecated ``garth``. Tokens are cached on
disk and last ~1 year, so only the first run needs the password and MFA code.
``garminconnect`` is an optional extra — imported lazily so the rest of the CLI
works without it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..models import HeartRateSeries, RawPayload

DEFAULT_TOKEN_DIR = "~/.garminconnect"

# Descriptor keys we need. Exact match first, then a case-insensitive substring
# fallback, since Garmin's key set shifts a little across activity types.
_HR_KEY, _HR_HINT = "directHeartRate", "heartrate"
_TS_KEY, _TS_HINT = "directTimestamp", "timestamp"

# maxChartSize caps returned samples and downsamples above the cap, so ask for
# comfortably more than one sample per second of activity.
_CHART_HEADROOM = 1.25
_MIN_CHART = 2000
_MAX_CHART = 60000


class GarminAuthError(RuntimeError):
    """Garmin Connect login failed (bad credentials, MFA, or rate limit)."""


class GarminClient:
    device = "garmin"

    def __init__(
        self,
        *,
        email: str | None = None,
        password: str | None = None,
        token_dir: str = DEFAULT_TOKEN_DIR,
        prompt_mfa: Callable[[], str] | None = None,
        api: Any | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._token_dir = token_dir
        self._prompt_mfa = prompt_mfa or (lambda: input("Garmin MFA code: ").strip())
        self._api = api  # injectable for tests

    def list_activities(
        self, start: date, end: date | None = None
    ) -> list[dict[str, Any]]:
        """Activities whose *local* start date falls in ``[start, end]``."""
        return self._connect().get_activities_by_date(
            start.isoformat(), (end or start).isoformat()
        )

    def fetch_activity_hr(
        self, activity_id: str | int, *, duration_s: float | None = None
    ) -> HeartRateSeries:
        """Per-sample HR for one activity.

        ``duration_s`` (from the activity summary) sizes ``maxChartSize`` so the
        series isn't silently downsampled; without it we fall back to a floor
        that covers roughly a half-hour at 1 Hz.
        """
        details = self._connect().get_activity_details(
            str(activity_id), maxchart=_chart_size(duration_s), maxpoly=0
        )
        raw = RawPayload(
            endpoint=f"activity-service/activity/{activity_id}/details",
            date=None,
            payload=details,
        )
        return HeartRateSeries(samples=_parse_details(details), raw=raw)

    def _connect(self) -> Any:
        if self._api is not None:
            return self._api
        try:
            from garminconnect import Garmin
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError(
                "garminconnect missing. Install with `uv sync --extra garmin`"
            ) from exc

        token_path = str(Path(self._token_dir).expanduser())
        # Cached tokens last ~1 year, so the password is only needed on first run.
        try:
            api = Garmin()
            api.login(token_path)
        except Exception:
            if not self._email or not self._password:
                raise GarminAuthError(
                    "No cached Garmin tokens and no credentials. Set GARMIN_EMAIL "
                    "/ GARMIN_PASSWORD in .env."
                )
            try:
                api = Garmin(
                    email=self._email,
                    password=self._password,
                    prompt_mfa=self._prompt_mfa,
                )
                api.login(token_path)
            except Exception as exc:
                # Never echo the request — it carries the account password.
                raise GarminAuthError(
                    f"Garmin sign-in failed ({type(exc).__name__}). Check "
                    "GARMIN_EMAIL / GARMIN_PASSWORD in .env. A 429 here means "
                    "Garmin is rate-limiting logins — wait before retrying."
                ) from exc
        self._api = api
        return api


def activity_start_utc(activity: dict[str, Any]) -> datetime | None:
    """Parse an activity summary's ``startTimeGMT`` (``YYYY-MM-DD HH:MM:SS``)."""
    raw = activity.get("startTimeGMT")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _chart_size(duration_s: float | None) -> int:
    if not duration_s or duration_s <= 0:
        return _MIN_CHART
    return max(_MIN_CHART, min(_MAX_CHART, int(duration_s * _CHART_HEADROOM)))


def _descriptor_index(details: dict[str, Any], key: str, hint: str) -> int | None:
    """Resolve a metric's position in the positional ``metrics`` array."""
    exact: int | None = None
    fuzzy: int | None = None
    for descriptor in details.get("metricDescriptors") or []:
        name = descriptor.get("key")
        index = descriptor.get("metricsIndex")
        if (
            not isinstance(name, str)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
        ):
            continue
        if name == key:
            exact = index
        elif hint in name.lower() and fuzzy is None:
            fuzzy = index
    return exact if exact is not None else fuzzy


def _parse_details(details: dict[str, Any]) -> list[tuple[datetime, int]]:
    """``activityDetailMetrics`` -> ``(utc_datetime, bpm)``, gaps dropped.

    A manually-entered activity (typed into Garmin Connect, never recorded by a
    device) has no HR descriptor at all — that returns empty rather than
    raising, since "this activity has no heart rate" is a legitimate answer.
    """
    hr_index = _descriptor_index(details, _HR_KEY, _HR_HINT)
    ts_index = _descriptor_index(details, _TS_KEY, _TS_HINT)
    if hr_index is None or ts_index is None:
        return []

    samples: list[tuple[datetime, int]] = []
    for sample in details.get("activityDetailMetrics") or []:
        metrics = sample.get("metrics") or []
        if hr_index >= len(metrics) or ts_index >= len(metrics):
            continue
        bpm, ts_ms = metrics[hr_index], metrics[ts_index]
        # Garmin leaves a null in the channel wherever the strap lost contact.
        if not isinstance(bpm, (int, float)) or not isinstance(ts_ms, (int, float)):
            continue
        if bpm <= 0:
            continue
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        samples.append((ts, round(bpm)))
    samples.sort(key=lambda pair: pair[0])
    return samples
