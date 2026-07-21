"""Shared OAuth 2.0 plumbing for the Whoop and Google Health clients.

Both providers use authorization-code flow with refresh tokens. ``TokenManager``
lazily refreshes an access token from the refresh token, and persists rotated
refresh tokens back to ``.env`` so the next run picks them up automatically.

This module is intentionally storage-light: only ``.env`` is touched. The
caller wires the right ``token_endpoint`` / ``env_key`` per provider.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx


def update_env_var(path: Path, key: str, value: str) -> None:
    """Set ``KEY=value`` in ``path`` (creating the line if missing), atomically.

    Lines that don't start with ``KEY=`` are preserved verbatim, so comments
    and ordering survive. Writes to a sibling temp file then ``os.replace``.
    """
    path = Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    prefix = f"{key}="
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(prefix):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.replace(tmp, path)


class TokenManager:
    """Holds a refresh token; mints access tokens on demand.

    Refresh tokens may rotate on each refresh (Whoop's behavior is
    undocumented at the time of writing). If the token endpoint returns a
    new ``refresh_token``, we persist it back to ``.env`` via
    ``update_env_var`` so the next process run picks it up.
    """

    # Skew so we refresh slightly before actual expiry.
    _EXPIRY_SKEW_SECONDS = 30

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_endpoint: str,
        env_key: str,
        env_path: Path,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.token_endpoint = token_endpoint
        self.env_key = env_key
        self.env_path = Path(env_path)
        self._http = http_client
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_access_token(self) -> str:
        if (
            self._access_token is not None
            and time.time() < self._expires_at - self._EXPIRY_SKEW_SECONDS
        ):
            return self._access_token
        self._refresh()
        assert self._access_token is not None
        return self._access_token

    def _refresh(self) -> None:
        body = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        self._apply_token_response(body)

    def _apply_token_response(self, body: dict[str, Any]) -> None:
        self._access_token = body["access_token"]
        self._expires_at = time.time() + body.get("expires_in", 3600)
        new_refresh = body.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            update_env_var(self.env_path, self.env_key, new_refresh)

    def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        if self._http is not None:
            resp = self._http.post(self.token_endpoint, data=data)
        else:
            resp = httpx.post(self.token_endpoint, data=data, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
