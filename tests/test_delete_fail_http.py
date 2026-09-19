"""HTTP contract: single delete is a controlled, recoverable move."""

from __future__ import annotations

import json
import os
import stat
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
    root = tmp_path / "root"
    album = root / "album"
    album.mkdir(parents=True)
    locked = album / "locked.bin"
    locked.write_bytes(b"lock")
    normal = album / "ok.bin"
    normal.write_bytes(b"ok")
    album.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    assert mb.replace_scan_root(str(root.resolve())) is True
    mb.reset_recycle_for_tests()
    server = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield port, str(locked), str(normal), album
    finally:
        album.chmod(0o755)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        safe = tmp_path / "safe"
        safe.mkdir(exist_ok=True)
        mb.replace_scan_root(str(safe.resolve()))


def test_readonly_parent_rejects_move_and_keeps_source(http_delete_port):
    port, locked, _, _ = http_delete_port
    code, body = _post_delete(port, locked)
    assert code == 403
    assert body["code"] in ("SOURCE_PARENT_READ_ONLY", "RECYCLE_MOVE_FAILED")
    assert os.path.isfile(locked)


def test_successful_delete_moves_file_and_preserves_folder(http_delete_port):
    port, _, normal, album = http_delete_port
    album.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    code, body = _post_delete(port, normal, str(album))
    assert code == 200
    assert body["ok"] is True
    assert body["folder_removed"] is False
    assert not os.path.isfile(normal)
    assert os.path.isdir(album)


def _post_delete(port: int, path: str, work_path: str = ""):
    data = json.dumps({"path": path, "work_path": work_path}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/delete",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
