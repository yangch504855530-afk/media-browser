"""QA：废纸篓 API 的 HTTP 契约（Handler + urllib，不依赖浏览器）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import media_browser as mb


@pytest.fixture()
def http_trash_port(tmp_path, monkeypatch):
    """独立 CACHE_DIR + 扫描根 + 队列预填 + HTTP 服务。"""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    album = tmp_path / "album"
    album.mkdir()
    f1 = album / "a.bin"
    f2 = album / "b.bin"
    f1.write_bytes(b"a")
    f2.write_bytes(b"b")
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True

    mb.delete_trash_clear()
    mb.delete_trash_add(str(f1), "simulated_fail")
    mb.delete_trash_add(str(f2), "simulated_fail")

    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield port, str(f1), str(f2)
    finally:
        srv.shutdown()
        srv.server_close()
        safe = tempfile.mkdtemp()
        mb.replace_scan_root(safe)


def _post_json(port: int, path: str, payload: dict) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_get_delete_trash_json(http_trash_port):
    port, f1, f2 = http_trash_port
    url = f"http://127.0.0.1:{port}/api/delete-trash"
    with urllib.request.urlopen(url, timeout=15) as resp:
        assert resp.status == 200
        assert "application/json" in resp.headers.get("Content-Type", "")
        body = json.loads(resp.read().decode("utf-8"))
    assert body.get("ok") is True
    assert body.get("count") == 2
    assert isinstance(body.get("items"), list)
    paths = {it["path"] for it in body["items"]}
    assert paths == {f1, f2}
    for it in body["items"]:
        assert "last_error" in it
        assert "path" in it


def test_post_delete_selected_http(http_trash_port):
    port, f1, f2 = http_trash_port
    status, out = _post_json(port, "/api/delete-trash/delete-selected", {"paths": [f1]})
    assert status == 200
    assert out.get("ok") is True
    assert out.get("deleted") == 1
    assert out.get("remaining") == 1
    assert not os.path.isfile(f1)
    assert os.path.isfile(f2)

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/delete-trash", timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body.get("count") == 1
    assert body["items"][0]["path"] == f2


def test_post_delete_selected_not_in_queue_http(http_trash_port):
    port, f1, f2 = http_trash_port
    outsider = str(Path(f1).parent.parent / "not_in_queue.bin")
    Path(outsider).write_bytes(b"n")
    status, out = _post_json(
        port,
        "/api/delete-trash/delete-selected",
        {"paths": [outsider]},
    )
    assert status == 200
    assert out.get("deleted") == 0
    assert out.get("skipped")
    assert any(x.get("error") == "not_in_trash_queue" for x in out["skipped"])
    assert os.path.isfile(outsider)


def test_post_delete_selected_invalid_body_returns_400(http_trash_port):
    port, _, _ = http_trash_port
    url = f"http://127.0.0.1:{port}/api/delete-trash/delete-selected"
    data = b"{"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=15)
    assert ei.value.code == 400
