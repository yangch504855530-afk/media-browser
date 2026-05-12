import shutil

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "ffmpeg: needs ffmpeg/ffprobe in PATH")


@pytest.fixture
def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
