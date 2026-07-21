from __future__ import annotations

from pathlib import Path

from wearable_pipeline.clients.google_health import GoogleHealthClient
from wearable_pipeline.clients.oura import OuraClient
from wearable_pipeline.clients.whoop import WhoopClient


def test_client_device_names(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert OuraClient(personal_access_token="x").device == "oura"
    assert (
        WhoopClient(
            client_id="a",
            client_secret="b",
            refresh_token="c",
            env_path=env,
        ).device
        == "whoop"
    )
    assert (
        GoogleHealthClient(
            client_id="a",
            client_secret="b",
            refresh_token="c",
            env_path=env,
        ).device
        == "google_health"
    )
