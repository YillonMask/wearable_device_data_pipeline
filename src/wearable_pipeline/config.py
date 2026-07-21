from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from CWD; idempotent. We capture the path so OAuth flows can
# rewrite rotated refresh tokens back to the same file.
ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class Settings:
    database_path: Path
    env_path: Path
    local_timezone: str
    oura_pat: str | None
    whoop_client_id: str | None
    whoop_client_secret: str | None
    whoop_redirect_uri: str | None
    whoop_refresh_token: str | None
    google_client_id: str | None
    google_client_secret: str | None
    google_redirect_uri: str | None
    google_refresh_token: str | None


def _opt(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def load_settings() -> Settings:
    return Settings(
        database_path=Path(os.environ.get("DATABASE_PATH", "data/wearable.db")),
        env_path=ENV_PATH,
        local_timezone=os.environ.get("LOCAL_TIMEZONE", "America/Los_Angeles"),
        oura_pat=_opt("OURA_PERSONAL_ACCESS_TOKEN"),
        whoop_client_id=_opt("WHOOP_CLIENT_ID"),
        whoop_client_secret=_opt("WHOOP_CLIENT_SECRET"),
        whoop_redirect_uri=_opt("WHOOP_REDIRECT_URI"),
        whoop_refresh_token=_opt("WHOOP_REFRESH_TOKEN"),
        google_client_id=_opt("GOOGLE_HEALTH_CLIENT_ID"),
        google_client_secret=_opt("GOOGLE_HEALTH_CLIENT_SECRET"),
        google_redirect_uri=_opt("GOOGLE_HEALTH_REDIRECT_URI"),
        google_refresh_token=_opt("GOOGLE_HEALTH_REFRESH_TOKEN"),
    )
