from __future__ import annotations

from pathlib import Path

import pytest

from wearable_pipeline import db


@pytest.fixture
def migrated_db(tmp_path: Path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()
