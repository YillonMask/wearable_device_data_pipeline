"""Interactive OAuth flow for Google Health API.

Run by ``wearable auth google``. Same loopback-callback pattern as the Whoop
flow, with Google-specific endpoints and the ``access_type=offline`` +
``prompt=consent`` parameters that Google requires to return a refresh token.

The OAuth consent screen must be in **Testing mode** with the user added as
a test user — otherwise the Restricted Google Health scopes require a full
CASA review.
"""

from __future__ import annotations

import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Scope strings confirmed against the Google Health API v4 discovery document.
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
)
_CALLBACK_TIMEOUT_SECONDS = 600


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    authorize_endpoint: str = AUTHORIZE_ENDPOINT,
) -> str:
    # access_type=offline + prompt=consent guarantee a refresh_token in the
    # token response. Without them Google may omit it on subsequent grants.
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "access_type": "offline",
            # `select_account` forces the account picker so reruns can switch
            # accounts; `consent` re-shows the scope screen and guarantees a
            # refresh_token on every successful flow.
            "prompt": "select_account consent",
            "include_granted_scopes": "true",
        }
    )
    return f"{authorize_endpoint}?{query}"


def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    token_endpoint: str = TOKEN_ENDPOINT,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if http_client is not None:
        resp = http_client.post(token_endpoint, data=data)
    else:
        resp = httpx.post(token_endpoint, data=data, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    if "refresh_token" not in body:
        raise RuntimeError(
            "Google token response missing refresh_token. Ensure the OAuth "
            "consent screen is in Testing mode and that you re-consented "
            "(prompt=consent) when going through the flow."
        )
    return body


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.result = {k: v[0] for k, v in params.items()}  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;text-align:center;"
            b"margin-top:6em;'><h2>Google auth complete.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *_args: Any) -> None:
        return


def run_interactive_flow(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    authorize_endpoint: str = AUTHORIZE_ENDPOINT,
    token_endpoint: str = TOKEN_ENDPOINT,
) -> str:
    """End-to-end interactive Google OAuth flow. Returns the refresh token."""
    state = secrets.token_urlsafe(24)
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765

    server = http.server.HTTPServer((host, port), _CallbackHandler)
    server.result = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        scopes=scopes,
        authorize_endpoint=authorize_endpoint,
    )
    print(f"Opening browser. If nothing opens, visit:\n  {url}", flush=True)
    webbrowser.open(url)

    thread.join(timeout=_CALLBACK_TIMEOUT_SECONDS)
    server.server_close()

    result = getattr(server, "result", None)
    if not result:
        raise RuntimeError(
            f"OAuth callback timed out after {_CALLBACK_TIMEOUT_SECONDS}s."
        )
    if "error" in result:
        raise RuntimeError(
            f"OAuth error: {result.get('error_description') or result['error']}"
        )
    if result.get("state") != state:
        raise RuntimeError("OAuth state mismatch — possible CSRF; aborting.")

    body = exchange_code(
        code=result["code"],
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        token_endpoint=token_endpoint,
    )
    return body["refresh_token"]
