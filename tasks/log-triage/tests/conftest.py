"""Hidden tests import the agent's code from APP_DIR (default /app)."""

import os
import sys
from pathlib import Path

import pytest

APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
sys.path.insert(0, str(APP_DIR))


@pytest.fixture
def app_dir() -> Path:
    return APP_DIR
