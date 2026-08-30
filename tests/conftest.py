import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures.build_fixture import build  # noqa: E402
from meetily_connector.config import Config  # noqa: E402


@pytest.fixture
def fixture_db(tmp_path):
    path = tmp_path / "meeting_minutes.sqlite"
    build(path)
    return path


@pytest.fixture
def cfg(tmp_path, fixture_db):
    c = Config()
    c.db_path = str(fixture_db)
    c.state_dir = tmp_path / "state"
    c.webhook_url = "http://127.0.0.1:9/webhooks/test"  # port 9: never connects
    c.settle_seconds = 30
    return c
