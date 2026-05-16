"""QA：POST /delete 失败入废纸篓队列的 HTTP 契约。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import urllib.error
import urllib.request

import pytest

import media_browser as mb


@pytest.fixture()
def http_delete_port(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    album = tmp_path / "album"
    album.mkdir()
    locked = album / "locked.bin"
    locked.write_bytes(b"lock")
    normal = album / "ok.bin"
    normal.write_bytes(b"ok")
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    mb.delete_trash_clear()

    album.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)

    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield port, str(locked), str(normal), album
    finally:
        album.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        srv.shutdown()
        srv.server_close()
        safe = tempfile.mkdtemp()
        mb.replace_scan_root(safe)


def _post_delete(port: int, path: str) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}/delete"
    data = json.dumps({"path": path}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        return e.code, body


def test_delete_readonly_dir_queues_trash(http_delete_port):
    port, locked, _, _ = http_delete_port
    code, body = _post_delete(port, locked)
    assert code == 500
    assert body.get("ok") is False
    assert body.get("queued") is True
    assert body.get("trash_count", 0) >= 1
    assert os.path.isfile(locked)

    url = f"http://127.0.0.1:{port}/api/delete-trash"
    with urllib.request.urlopen(url, timeout=15) as resp:
        listed = json.loads(resp.read().decode("utf-8"))
    assert listed.get("count") == 1
    assert listed["items"][0]["path"] == locked


def test_delete_success_removes_file_and_not_in_trash(http_delete_port):
    port, _, normal, album = http_delete_port
    album.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    code, body = _post_delete(port, normal)
    assert code == 200
    assert body.get("ok") is True
    assert not os.path.isfile(normal)

    url = f"http://127.0.0.1:{port}/api/delete-trash"
    with urllib.request.urlopen(url, timeout=15) as resp:
        listed = json.loads(resp.read().decode("utf-8"))
    assert listed.get("count") == 0
