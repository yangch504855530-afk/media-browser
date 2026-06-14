"""审阅账本 review_state — NAS cache 持久化与 API。"""

import json
import threading
import urllib.parse
import urllib.request

import pytest

import media_browser as mb


@pytest.fixture()
def review_server(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    work = tmp_path / "album"
    work.mkdir()
    (work / "a.mp4").write_bytes(b"x")
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


def test_review_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    wid = "abc123"
    ret = mb.patch_review_state_work(
        wid,
        {
            "tag": "kept",
            "last_item_path": "/scan/foo.mp4",
            "last_item_idx": 2,
            "update_global": True,
        },
    )
    assert ret["ok"] is True
    assert ret["work"]["tag"] == "kept"
    state = mb.load_review_state()
    assert state["works"][wid]["tag"] == "kept"
    assert state["global"]["last_work_id"] == wid
    assert state["works"][wid]["last_item_path"] == "/scan/foo.mp4"


def test_review_state_supports_deferred_delete_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True

    ret = mb.patch_review_state_work(
        "delete-later",
        {"tag": "deleted", "update_global": False},
    )

    assert ret["ok"] is True
    assert mb.load_review_state()["works"]["delete-later"]["tag"] == "deleted"


def test_review_state_http_get_and_patch(review_server):
    port = review_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/review-state", timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body["ok"] is True
    assert "state" in body
    wid = "deadbeef"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/review-state/work/{wid}",
        data=json.dumps({"tag": "pending", "update_global": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        patched = json.loads(resp.read().decode("utf-8"))
    assert patched["ok"] is True
    assert patched["work"]["tag"] == "pending"


def test_import_review_tags_skips_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    mb.patch_review_state_work("w1", {"tag": "kept", "update_global": False})
    out = mb.import_review_tags({"w1": "pending", "w2": "kept"})
    assert out["imported"] == 1
    state = mb.load_review_state()
    assert state["works"]["w1"]["tag"] == "kept"
    assert state["works"]["w2"]["tag"] == "kept"


def test_clear_review_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    mb.patch_review_state_work("w1", {"tag": "kept", "update_global": False})
    mb.clear_review_state()
    state = mb.load_review_state()
    assert state["works"] == {}
