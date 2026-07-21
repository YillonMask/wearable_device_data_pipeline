"""Interactive OAuth flow for Whoop.

Run by ``wearable auth whoop``. Spawns a local HTTP server on the loopback
interface, opens the browser to Whoop's authorize URL, captures the callback,
exchanges the code for tokens, and returns the refresh token. The caller is
responsible for persisting the refresh token to ``.env``.
"""

from __future__ import annotations

import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx

AUTHORIZE_ENDPOINT = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_ENDPOINT = "https://api.prod.whoop.com/oauth/oauth2/token"
DEFAULT_SCOPES = (
    # `offline` is required for Whoop to return a refresh_token. Without it
    # the token endpoint only emits an access_token + expires_in.
    "offline",
    "read:profile",
    "read:sleep",
    "read:recovery",
    "read:cycles",
    "read:workout",
    "read:body_measurement",
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
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
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
    """Exchange an authorization code for a token bundle.

    Returns the full JSON body from the token endpoint. Caller extracts the
    ``refresh_token`` (raising if the provider didn't return one).
    """
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
            "Whoop token response missing refresh_token; got keys: "
            f"{sorted(body.keys())}. Check that your registered app includes "
            "the offline_access / refresh-token grants."
        )
    return body


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single OAuth redirect on the loopback interface."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.result = {k: v[0] for k, v in params.items()}  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;text-align:center;"
            b"margin-top:6em;'><h2>Whoop auth complete.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *_args: Any) -> None:
        return  # silence stderr access log


def run_interactive_flow(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    authorize_endpoint: str = AUTHORIZE_ENDPOINT,
    token_endpoint: str = TOKEN_ENDPOINT,
) -> str:
    """End-to-end interactive flow. Returns the refresh token."""
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
        raise RuntimeError(f"OAuth error: {result.get('error_description') or result['error']}")
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
