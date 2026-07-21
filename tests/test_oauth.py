from __future__ import annotations

from pathlib import Path

import pytest
import respx

from wearable_pipeline.clients._oauth import TokenManager, update_env_var

TOKEN_URL = "https://example.com/oauth/token"


# --- update_env_var ----------------------------------------------------------


def test_update_env_var_creates_file_when_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    update_env_var(env, "TOKEN", "abc123")
    assert env.read_text() == "TOKEN=abc123\n"


def test_update_env_var_replaces_existing_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OTHER=keep\nTOKEN=old\nANOTHER=also_keep\n")
    update_env_var(env, "TOKEN", "new")
    assert env.read_text() == "OTHER=keep\nTOKEN=new\nANOTHER=also_keep\n"


def test_update_env_var_appends_when_key_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OTHER=keep\n")
    update_env_var(env, "TOKEN", "new")
    assert env.read_text() == "OTHER=keep\nTOKEN=new\n"


def test_update_env_var_preserves_comments_and_blank_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# leading comment\n\nTOKEN=old\n# trailing comment\nLAST=val\n"
    )
    update_env_var(env, "TOKEN", "new")
    out = env.read_text()
    assert "# leading comment" in out
    assert "# trailing comment" in out
    assert "TOKEN=new" in out
    assert "TOKEN=old" not in out
    assert out.endswith("LAST=val\n")


# --- TokenManager ------------------------------------------------------------


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("WHOOP_REFRESH_TOKEN=initial-refresh\n")
    return p


def _make_manager(env_file: Path, refresh: str = "initial-refresh") -> TokenManager:
    return TokenManager(
        client_id="cid",
        client_secret="csec",
        refresh_token=refresh,
        token_endpoint=TOKEN_URL,
        env_key="WHOOP_REFRESH_TOKEN",
        env_path=env_file,
    )


@respx.mock
def test_first_call_triggers_refresh(env_file: Path) -> None:
    route = respx.post(TOKEN_URL).respond(
        200,
        json={
            "access_token": "access-abc",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    mgr = _make_manager(env_file)
    assert mgr.get_access_token() == "access-abc"
    assert route.call_count == 1


@respx.mock
def test_subsequent_calls_cache_within_expiry(env_file: Path) -> None:
    route = respx.post(TOKEN_URL).respond(
        200, json={"access_token": "access-1", "expires_in": 3600}
    )
    mgr = _make_manager(env_file)
    mgr.get_access_token()
    mgr.get_access_token()
    mgr.get_access_token()
    assert route.call_count == 1


@respx.mock
def test_rotated_refresh_token_is_persisted(env_file: Path) -> None:
    respx.post(TOKEN_URL).respond(
        200,
        json={
            "access_token": "access-1",
            "refresh_token": "rotated-refresh",
            "expires_in": 3600,
        },
    )
    mgr = _make_manager(env_file)
    mgr.get_access_token()
    assert mgr.refresh_token == "rotated-refresh"
    assert "WHOOP_REFRESH_TOKEN=rotated-refresh" in env_file.read_text()


@respx.mock
def test_unchanged_refresh_token_does_not_rewrite_env(env_file: Path) -> None:
    respx.post(TOKEN_URL).respond(
        200,
        json={
            "access_token": "access-1",
            "refresh_token": "initial-refresh",  # echoed unchanged
            "expires_in": 3600,
        },
    )
    before = env_file.read_text()
    mgr = _make_manager(env_file)
    mgr.get_access_token()
    assert env_file.read_text() == before


@respx.mock
def test_token_request_form_payload(env_file: Path) -> None:
    route = respx.post(TOKEN_URL).respond(
        200, json={"access_token": "access-1", "expires_in": 3600}
    )
    _make_manager(env_file).get_access_token()
    sent = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in sent
    assert "refresh_token=initial-refresh" in sent
    assert "client_id=cid" in sent
    assert "client_secret=csec" in sent
