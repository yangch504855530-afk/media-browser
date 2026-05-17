"""QA：POST /api/works/delete-all — 批量删媒体并尝试移除空文件夹。"""

from __future__ import annotations

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
    root.mkdir()
    work = root / "album_a"
    work.mkdir()
    (work / "a.jpg").write_bytes(b"jpeg")
    (work / "b.jpg").write_bytes(b"jpeg2")
    assert mb.replace_scan_root(str(root.resolve())) is True
    mb.delete_trash_clear()

    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield port, str(work), [str(work / "a.jpg"), str(work / "b.jpg")]
    finally:
        srv.shutdown()
        srv.server_close()
        safe = tmp_path / "safe"
        safe.mkdir(exist_ok=True)
        mb.replace_scan_root(str(safe.resolve()))


def _post_delete_all(port: int, work_path: str, paths: list[str]) -> dict:
    url = f"http://127.0.0.1:{port}/api/works/delete-all"
    data = json.dumps({"work_path": work_path, "paths": paths}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_delete_work_all_removes_media_and_empty_folder(work_delete_port):
    port, work_path, paths = work_delete_port
    body = _post_delete_all(port, work_path, paths)
    assert body.get("ok") is True
    assert body.get("deleted") == 2
    assert len(body.get("deleted_paths", [])) == 2
    assert body.get("folder_removed") is True
    assert not os.path.isdir(work_path)
    for p in paths:
        assert not os.path.isfile(p)


def test_delete_work_all_unit_try_remove_empty_folder(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    root = tmp_path / "scan"
    root.mkdir()
    work = root / "nested"
    work.mkdir()
    sub = work / "empty_sub"
    sub.mkdir()
    assert mb.replace_scan_root(str(root.resolve())) is True

    assert mb.try_remove_empty_work_folder(str(work)) is True
    assert not os.path.isdir(work)
