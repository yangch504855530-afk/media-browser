"""HTTP 首页与 /api/works 契约回归（嵌入式 Handler，不依赖外部浏览器）。"""

from __future__ import annotations

import html as html_mod
import json
import threading
import time
import urllib.request

import pytest

import media_browser as mb

_WORK_TOP_KEYS = {
    "ok",
    "works",
    "done",
    "scanned",
    "total",
    "enum_error",
    "scan_root",
}
_WORK_KEYS = {
    "id",
    "name",
    "path",
    "items",
    "thumbs",
    "video_count",
    "image_count",
    "total_size",
    "total_duration",
    "main_duration",
    "mtime",
}
_ITEM_REQUIRED = {"type", "path", "name", "size"}


def _build_homepage_bytes() -> bytes:
    page = (
        mb.HTML_PAGE.replace("__MB_ROOT_DIR__", html_mod.escape(mb.get_scan_root()))
        .replace("__THUMB_COUNT__", str(mb.THUMB_COUNT))
        .replace("__APP_VERSION__", html_mod.escape(mb.APP_VERSION))
        .replace("__MB_SCAN_READONLY__", "true" if mb.scan_root_readonly() else "false")
    )
    return page.encode("utf-8")


@pytest.fixture()
def http_server_port(tmp_path):
    """独立扫描根 + 本机 HTTP 服务，避免与其它用例共享 scanner 状态。"""
    album = tmp_path / "album1"
    album.mkdir()
    (album / "pic.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()
        import tempfile

        safe = tempfile.mkdtemp()
        mb.replace_scan_root(safe)


def test_homepage_returns_html(http_server_port):
    """静态 HTML 结构 + 实际 GET / 的 200 与 Content-Type。"""
    body = _build_homepage_bytes()
    text = body.decode("utf-8")
    assert text.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in text
    assert 'id="container"' in text
    assert "Media Browser" in text
    assert "function poll" in text or "poll();" in text

    url = f"http://127.0.0.1:{http_server_port}/"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/html" in ct
        assert "charset=utf-8" in ct.replace(" ", "").lower()
        raw = resp.read()
    assert raw.startswith(b"<!DOCTYPE html")
    assert b"id=\"container\"" in raw


def test_api_works_json_structure(http_server_port):
    deadline = time.monotonic() + 45.0
    data = None
    while time.monotonic() < deadline:
        url = f"http://127.0.0.1:{http_server_port}/api/works"
        with urllib.request.urlopen(url, timeout=15) as resp:
            assert resp.status == 200
            ct = resp.headers.get("Content-Type", "")
            assert "application/json" in ct
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("done"):
            break
        time.sleep(0.15)
    assert data is not None
    assert _WORK_TOP_KEYS <= set(data.keys())
    assert data.get("ok") is True
    assert isinstance(data["works"], list)
    assert isinstance(data["done"], bool)
    assert isinstance(data["scanned"], int)
    assert isinstance(data["total"], int)
    assert data["enum_error"] is None or isinstance(data["enum_error"], str)
    assert isinstance(data["scan_root"], str)

    assert len(data["works"]) >= 1
    for w in data["works"]:
        assert _WORK_KEYS <= set(w.keys())
        assert isinstance(w["id"], str)
        assert isinstance(w["name"], str)
        assert isinstance(w["path"], str)
        assert isinstance(w["items"], list)
        assert isinstance(w["thumbs"], list)
        for k in ("video_count", "image_count", "total_size"):
            assert isinstance(w[k], int)
        assert isinstance(w["mtime"], (int, float))
        assert isinstance(w["total_duration"], (int, float))
        assert isinstance(w["main_duration"], (int, float))
        for it in w["items"]:
            assert _ITEM_REQUIRED <= set(it.keys())
            assert it["type"] in ("video", "image")
            assert isinstance(it["path"], str)
            assert isinstance(it["name"], str)
            assert isinstance(it["size"], int)


def test_api_tags_returns_tags_array(http_server_port):
    url = f"http://127.0.0.1:{http_server_port}/api/tags"
    with urllib.request.urlopen(url, timeout=15) as resp:
        assert resp.status == 200
        payload = json.loads(resp.read().decode("utf-8"))
    assert "tags" in payload
    assert isinstance(payload["tags"], list)
    assert all(isinstance(x, str) for x in payload["tags"])
    assert "ok" in payload


def test_health_http_json_keys(http_server_port):
    url = f"http://127.0.0.1:{http_server_port}/health"
    with urllib.request.urlopen(url, timeout=15) as resp:
        assert resp.status in (200, 503)
        body = json.loads(resp.read().decode("utf-8"))
    for k in ("version", "disk", "scan", "ffmpeg", "ollama", "cache"):
        assert k in body
