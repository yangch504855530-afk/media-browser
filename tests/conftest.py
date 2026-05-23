import shutil

import pytest

pytest_plugins = ["conftest_gallery_e2e"]

import media_browser as mb


@pytest.fixture(autouse=True)
def _reset_scan_preset_state(monkeypatch):
    """各用例互不污染 MB_SCAN_PRESETS / 扫描根 / 硬件转码全局状态。"""
    monkeypatch.delenv("MB_SCAN_PRESETS", raising=False)
    monkeypatch.delenv("MB_AUTO_SCAN", raising=False)
    monkeypatch.setenv("MB_FFMPEG_HW", "off")
    mb._invalidate_ffmpeg_hw_cache()
    mb.bootstrap_scan_configuration()
    yield
    mb._invalidate_ffmpeg_hw_cache()


def pytest_configure(config):
    config.addinivalue_line("markers", "ffmpeg: needs ffmpeg/ffprobe in PATH")
    config.addinivalue_line("markers", "e2e: Playwright browser tests (needs playwright + chromium)")


@pytest.fixture
def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
