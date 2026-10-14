import os
import sys
from pathlib import Path

os.environ.setdefault("SANDBOX_TODAY", "2026-10-14")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(scope="session")
def sandbox_live():
    from sandbox import server
    server.start()
    yield server
