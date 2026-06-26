from __future__ import annotations

import json
import os

import media_browser as mb


def test_replace_cache_dir_persists_and_restarts_scan(tmp_path, monkeypatch):
    settings_dir = tmp_path / "settings"
    settings_path = settings_dir / "settings.json"
    cache_dir = tmp_path / "cache"
    started = []
    stopped = []

    class DummyExecutor:
        def shutdown(self, wait=False):
            stopped.append(wait)

    class OldScanner:
        _executor = DummyExecutor()

    class NewScanner:
        def __init__(self):
            self._executor = DummyExecutor()

        def start(self):
            started.append(True)

    monkeypatch.setattr(mb, "SETTINGS_DIR", str(settings_dir))
    monkeypatch.setattr(mb, "SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(mb, "_PERSISTENT_SETTINGS", {})
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "old-cache"))
    monkeypatch.setattr(mb, "PLACEHOLDER", os.path.join(str(tmp_path / "old-cache"), "_placeholder.jpg"))
    monkeypatch.setattr(mb, "scanner", OldScanner())
    monkeypatch.setattr(mb, "MediaScanner", NewScanner)

    ok, payload = mb.replace_cache_dir(str(cache_dir))

    assert ok is True
    assert payload["ok"] is True
    assert payload["cache_dir"] == str(cache_dir.resolve())
    assert mb.CACHE_DIR == str(cache_dir.resolve())
    assert started == [True]
    assert stopped == [False]
    assert json.loads(settings_path.read_text(encoding="utf-8"))["cache_dir"] == str(cache_dir.resolve())
    assert cache_dir.is_dir()
