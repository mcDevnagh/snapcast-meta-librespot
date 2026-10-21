import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def runtime_dir():
    """A short XDG_RUNTIME_DIR: socket paths are capped at 108 bytes, and
    pytest's tmp_path (named after the test) can push them over."""
    path = Path(tempfile.mkdtemp(prefix="sml-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)
