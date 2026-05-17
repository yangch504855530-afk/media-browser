"""扫描根目录路径解析：NAS /Volumes、smb:// 误填等。"""

from __future__ import annotations

import os
import sys
import pytest

import media_browser as mb


def test_resolve_smb_url_rejected():
    path, err = mb.resolve_scan_root_path("smb://192.168.1.1/share/photos")
    assert path is None
    assert err
    assert "Finder" in err or "⌘K" in err
    assert "/Volumes" in err


def test_resolve_file_url(tmp_path):
    p = tmp_path / "nas_root"
    p.mkdir()
    uri = p.as_uri()
    resolved, err = mb.resolve_scan_root_path(uri)
    assert err is None
    assert resolved is not None
    assert os.path.isdir(resolved)


def test_resolve_normal_path(tmp_path):
    p = tmp_path / "album"
    p.mkdir()
    resolved, err = mb.resolve_scan_root_path(str(p))
    assert err is None
    assert resolved == os.path.realpath(str(p.resolve()))


def test_check_volumes_directory_triggers_listdir(monkeypatch):
    """模拟 macOS NAS：首次 isdir 为 False，listdir 触发挂载后 isdir 为 True。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    p = "/Volumes/NAS/media"
    calls = {"n": 0}

    def fake_isdir(path):
        if path == p:
            calls["n"] += 1
            return calls["n"] > 1
        return False

    monkeypatch.setattr(os.path, "isdir", fake_isdir)
    monkeypatch.setattr(os, "listdir", lambda path: [] if path == p else (_ for _ in ()).throw(FileNotFoundError()))
    ok, err = mb._check_scan_root_directory(p)
    assert ok is True
    assert err is None


def test_replace_scan_root_uses_resolver(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    old = mb.scanner
    try:
        assert mb.replace_scan_root(str(root)) is True
        assert mb.get_scan_root() == os.path.realpath(str(root.resolve()))
    finally:
        mb.scanner = old


def test_set_scan_root_http_error_message(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.error
    import urllib.request

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True

    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{port}/api/set-scan-root"
        body = json.dumps({"path": "smb://nas/media"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 400
        payload = json.loads(ei.value.read().decode("utf-8"))
        assert payload.get("ok") is False
        assert "Volumes" in payload.get("error", "")
    finally:
        srv.shutdown()
        srv.server_close()
