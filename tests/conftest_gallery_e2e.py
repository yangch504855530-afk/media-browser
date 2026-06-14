"""Playwright 画廊 E2E 共用 fixture（由 test_gallery_e2e 引用）。"""

from __future__ import annotations

import tempfile
import threading

import pytest

import media_browser as mb


@pytest.fixture(scope="session")
def playwright_browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def gallery_e2e_server(tmp_path, monkeypatch):
    """双图作品 + 已启动扫描 + 本机 HTTP。"""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    album = tmp_path / "album_qa"
    album.mkdir()
    (album / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    (album / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True

    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{port}", album
    finally:
        srv.shutdown()
        srv.server_close()
        safe = tempfile.mkdtemp()
        mb.replace_scan_root(safe)


@pytest.fixture()
def gallery_two_work_e2e_server(tmp_path, monkeypatch):
    """Two ordered single-file works for automatic gallery continuation."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    first = tmp_path / "album_a"
    second = tmp_path / "album_b"
    first.mkdir()
    second.mkdir()
    (first / "first.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    (second / "second.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True

    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{port}", first, second
    finally:
        srv.shutdown()
        srv.server_close()
        safe = tempfile.mkdtemp()
        mb.replace_scan_root(safe)
