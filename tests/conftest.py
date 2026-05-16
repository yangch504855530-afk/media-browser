import shutil

import pytest

pytest_plugins = ["conftest_gallery_e2e"]


def pytest_configure(config):
    config.addinivalue_line("markers", "ffmpeg: needs ffmpeg/ffprobe in PATH")
    config.addinivalue_line("markers", "e2e: Playwright browser tests (needs playwright + chromium)")


@pytest.fixture
def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
