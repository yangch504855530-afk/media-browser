"""Batch delete only moves media and preserves directories."""

import json
import os
import threading
import urllib.request

import pytest

import media_browser as mb


@pytest.fixture()
def work_delete_port(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    root = tmp_path / "library"
    work = root / "album_a"
    work.mkdir(parents=True)
    paths = []
    for name, payload in (("a.jpg", b"jpeg"), ("b.jpg", b"jpeg2")):
        path = work / name
        path.write_bytes(payload)
        paths.append(str(path))
    assert mb.replace_scan_root(str(root.resolve())) is True
    mb.reset_recycle_for_tests()
    server = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], str(work), paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        safe = tmp_path / "safe"
        safe.mkdir(exist_ok=True)
        mb.replace_scan_root(str(safe.resolve()))


def test_delete_work_all_moves_media_and_keeps_folder(work_delete_port):
    port, work_path, paths = work_delete_port
    data = json.dumps({"work_path": work_path, "paths": paths}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/works/delete-all",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        body = json.loads(response.read())
    assert body["ok"] is True
    assert body["deleted"] == 2
    assert body["folder_removed"] is False
    assert os.path.isdir(work_path)
    assert all(not os.path.isfile(path) for path in paths)
