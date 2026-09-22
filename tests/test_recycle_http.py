"""Production-Handler HTTP tests for move-only recycle semantics."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import media_browser as mb


@pytest.fixture()
def recycle_server(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    root = tmp_path / "library"
    nested = root / "album"
    nested.mkdir(parents=True)
    media = nested / "clip.mp4"
    media.write_bytes(b"original-payload")
    other = nested / "other.mp4"
    other.write_bytes(b"other-payload")
    outsider = tmp_path / "outside.mp4"
    outsider.write_bytes(b"outside")
    assert mb.replace_scan_root(str(root.resolve())) is True
    deadline = time.monotonic() + 10
    while not mb.scanner.done and time.monotonic() < deadline:
        time.sleep(0.02)
    mb.reset_recycle_for_tests()

    server = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield {
            "port": port,
            "root": root.resolve(),
            "nested": nested,
            "media": media,
            "other": other,
            "outsider": outsider,
            "cache": cache.resolve(),
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        safe = tmp_path / "safe-final"
        safe.mkdir(exist_ok=True)
        mb.replace_scan_root(str(safe.resolve()))


def _post_json(port: int, path: str, payload, headers=None):
    data = b"{}" if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _recycle_items(port: int):
    _, body = _get_json(port, "/api/delete-trash")
    return body["items"]


def test_single_delete_moves_registers_and_restores_http(recycle_server):
    port = recycle_server["port"]
    media = recycle_server["media"]
    status, body = _post_json(port, "/delete", {"path": str(media), "work_path": str(media.parent)})
    assert status == 200, body
    assert body["ok"] is True
    assert body["already_recycled"] is False
    assert body["folder_removed"] is False
    assert not media.exists()

    items = _recycle_items(port)
    assert len(items) == 1
    item = items[0]
    assert item["original_root"] == str(recycle_server["root"])
    assert item["relative_path"].replace("\\", "/") == "album/clip.mp4"
    assert item["relative_parts"] == ["album", "clip.mp4"]
    assert item["filename"] == "clip.mp4"
    assert item["recycled_at"]
    assert item["status"] == "recycled"
    assert Path(item["object_path"]).is_file()
    assert recycle_server["cache"] in Path(item["object_path"]).resolve().parents

    repeat_status, repeat_body = _post_json(port, "/delete", {"path": str(media)})
    assert repeat_status == 200
    assert repeat_body["ok"] is True
    assert repeat_body["already_recycled"] is True
    assert repeat_body["id"] == item["id"]
    assert len(_recycle_items(port)) == 1

    restore_status, restore_body = _post_json(
        port, "/api/recycle/restore", {"ids": [item["id"]]}
    )
    assert restore_status == 200
    assert restore_body["restored_count"] == 1
    assert restore_body["remaining"] == 0
    assert media.read_bytes() == b"original-payload"

    repeat_restore_status, repeat_restore = _post_json(
        port, "/api/recycle/restore", {"ids": [item["id"]]}
    )
    assert repeat_restore_status == 200
    assert repeat_restore["restored_count"] == 1
    assert repeat_restore["restored"][0]["already_restored"] is True
    assert media.read_bytes() == b"original-payload"


def test_restore_renames_on_target_conflict_http(recycle_server):
    port = recycle_server["port"]
    media = recycle_server["media"]
    _, move = _post_json(port, "/delete", {"path": str(media)})
    media.write_bytes(b"newer-file")
    status, body = _post_json(port, "/api/recycle/restore", {"ids": [move["id"]]})
    assert status == 200
    assert body["restored_count"] == 1
    restored = body["restored"][0]
    assert restored["restore_conflict_renamed"] is True
    assert restored["restored_path"] != str(media)
    assert media.read_bytes() == b"newer-file"
    assert Path(restored["restored_path"]).read_bytes() == b"original-payload"


def test_readonly_parent_is_rejected_without_moving_http(recycle_server):
    port = recycle_server["port"]
    nested = recycle_server["nested"]
    media = recycle_server["media"]
    nested.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    try:
        status, body = _post_json(port, "/delete", {"path": str(media)})
        assert status == 403
        assert body["ok"] is False
        assert body["code"] in ("SOURCE_PARENT_READ_ONLY", "RECYCLE_MOVE_FAILED")
        assert media.read_bytes() == b"original-payload"
        assert _recycle_items(port) == []
    finally:
        nested.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def test_explicit_media_readonly_guard_blocks_move_http(recycle_server, monkeypatch):
    port = recycle_server["port"]
    media = recycle_server["media"]
    monkeypatch.setenv("MB_MEDIA_READONLY", "1")
    status, body = _post_json(port, "/delete", {"path": str(media)})
    assert status == 403
    assert body["code"] == "MEDIA_READONLY"
    assert media.read_bytes() == b"original-payload"
    assert _recycle_items(port) == []


def test_delete_outside_root_is_forbidden_http(recycle_server):
    port = recycle_server["port"]
    outsider = recycle_server["outsider"]
    status, body = _post_json(port, "/delete", {"path": str(outsider)})
    assert status == 403
    assert body["code"] == "PATH_OUTSIDE_ROOT"
    assert outsider.read_bytes() == b"outside"
    assert _recycle_items(port) == []


def test_batch_delete_moves_all_and_repeats_idempotently_http(recycle_server):
    port = recycle_server["port"]
    nested = recycle_server["nested"]
    media = recycle_server["media"]
    other = recycle_server["other"]
    paths = [str(media), str(other)]
    status, body = _post_json(
        port,
        "/api/works/delete-all",
        {"work_path": str(nested), "paths": paths},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["deleted"] == 2
    assert body["folder_removed"] is False
    assert nested.is_dir()
    assert not media.exists() and not other.exists()
    assert len(_recycle_items(port)) == 2

    repeat_status, repeat = _post_json(
        port,
        "/api/works/delete-all",
        {"work_path": str(nested), "paths": paths},
    )
    assert repeat_status == 200
    assert repeat["deleted"] == 0
    assert repeat["errors"] == []
    assert len(_recycle_items(port)) == 2


def test_restore_rejects_metadata_path_escape_http(recycle_server):
    port = recycle_server["port"]
    media = recycle_server["media"]
    _, move = _post_json(port, "/delete", {"path": str(media)})
    manifest = Path(mb._delete_trash_store_path())
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["items"][0]["relative_path"] = "../escaped.mp4"
    data["items"][0]["relative_parts"] = ["..", "escaped.mp4"]
    manifest.write_text(json.dumps(data), encoding="utf-8")

    status, body = _post_json(port, "/api/recycle/restore", {"ids": [move["id"]]})
    assert status == 200
    assert body["restored_count"] == 0
    assert body["skipped"][0]["code"] in ("INVALID_RECYCLE_PATH", "PATH_OUTSIDE_ROOT")
    assert not (recycle_server["root"].parent / "escaped.mp4").exists()
    assert len(_recycle_items(port)) == 1
    assert not media.exists()


def test_purge_requires_explicit_double_confirmation_http(recycle_server):
    port = recycle_server["port"]
    media = recycle_server["media"]
    _, move = _post_json(port, "/delete", {"path": str(media)})
    object_path = Path(_recycle_items(port)[0]["object_path"])

    default_status, default_body = _post_json(port, "/api/delete-trash/clear", {})
    assert default_status == 403
    assert default_body["code"] == "PURGE_REQUIRES_CONFIRM"
    assert object_path.is_file()

    body_only_status, _ = _post_json(
        port, "/api/delete-trash/clear", {"confirm": "PURGE"}
    )
    assert body_only_status == 403
    header_only_status, _ = _post_json(
        port,
        "/api/delete-trash/clear",
        {},
        headers={"X-MB-Recycle-Confirm": "PURGE"},
    )
    assert header_only_status == 403

    purge_status, purge_body = _post_json(
        port,
        "/api/delete-trash/clear",
        {"confirm": "PURGE"},
        headers={"X-MB-Recycle-Confirm": "PURGE"},
    )
    assert purge_status == 200
    assert purge_body["purged_count"] == 1
    assert not object_path.exists()
    assert _recycle_items(port) == []
    assert not media.exists()
