"""Recycle HTTP routes cannot permanently delete silently."""

import json
import threading
import urllib.error
import urllib.request

import pytest

import media_browser as mb


@pytest.fixture()
def http_trash_port(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    root = tmp_path / "root"
    root.mkdir()
    media = root / "a.bin"
    media.write_bytes(b"a")
    assert mb.replace_scan_root(str(root.resolve())) is True
    mb.reset_recycle_for_tests()
    server = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], media
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        safe = tmp_path / "safe"
        safe.mkdir(exist_ok=True)
        mb.replace_scan_root(str(safe.resolve()))


def _post(port, path, payload=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload or {}).encode(),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_delete_selected_without_restore_is_forbidden(http_trash_port):
    port, media = http_trash_port
    mb.move_media_to_recycle(str(media))
    status, body = _post(port, "/api/delete-trash/delete-selected", {"paths": [str(media)]})
    assert status == 409
    assert body["code"] == "RECYCLE_DELETE_FORBIDDEN"


def test_clear_default_rejects_and_purge_requires_double_confirm(http_trash_port):
    port, media = http_trash_port
    mb.move_media_to_recycle(str(media))
    status, body = _post(port, "/api/delete-trash/clear", {})
    assert status == 403
    assert body["code"] == "PURGE_REQUIRES_CONFIRM"
    status, body = _post(port, "/api/delete-trash/clear", {"confirm": "PURGE"}, {"X-MB-Recycle-Confirm": "PURGE"})
    assert status == 200
    assert body["purged_count"] == 1
