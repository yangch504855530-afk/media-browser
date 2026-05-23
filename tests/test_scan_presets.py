"""MB_SCAN_PRESETS：Docker 媒体库白名单与启动不自动扫描。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import media_browser as mb


@pytest.fixture()
def preset_env(tmp_path, monkeypatch):
    lib_a = tmp_path / "lib_a"
    lib_b = tmp_path / "lib_b"
    lib_a.mkdir()
    lib_b.mkdir()
    (lib_a / "w1").mkdir()
    (lib_a / "w1" / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    (lib_b / "w2").mkdir()
    (lib_b / "w2" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    presets = f"{lib_a}|库A;{lib_b}|库B"
    monkeypatch.setenv("MB_SCAN_PRESETS", presets)
    monkeypatch.setenv("MB_ROOT_DIR", str(lib_a))
    monkeypatch.setenv("MB_AUTO_SCAN", "0")
    mb.bootstrap_scan_configuration()
    idle_scanner = mb.MediaScanner()
    idle_scanner.mark_idle()
    monkeypatch.setattr(mb, "scanner", idle_scanner)
    monkeypatch.setattr(mb, "_scan_root", str(lib_a.resolve()))
    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield port, lib_a, lib_b
    finally:
        srv.shutdown()
        srv.server_close()


def test_parse_and_bootstrap_presets(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv(
        "MB_SCAN_PRESETS",
        f"{a}|Alpha;{b}|Beta",
    )
    monkeypatch.setenv("MB_ROOT_DIR", str(b))
    mb.bootstrap_scan_configuration()
    assert mb.scan_presets_enabled()
    presets = mb.get_scan_presets()
    assert len(presets) == 2
    assert presets[1]["label"] == "Beta"
    assert mb.get_scan_root() == str(b.resolve())


def test_preset_reject_unknown_path(preset_env, tmp_path):
    port, _lib_a, _lib_b = preset_env
    outside = tmp_path / "outside_lib"
    outside.mkdir()
    url = f"http://127.0.0.1:{port}/api/set-scan-root"
    body = json.dumps({"path": str(outside)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 403
    payload = json.loads(exc.value.read().decode("utf-8"))
    assert payload.get("ok") is False
    assert "媒体库" in (payload.get("error") or "")


def test_preset_switch_starts_scan(preset_env):
    port, _lib_a, lib_b = preset_env
    url = f"http://127.0.0.1:{port}/api/set-scan-root"
    body = json.dumps({"path": str(lib_b)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data.get("ok") is True
    prog = mb.scanner.get_progress(0)
    assert prog.get("idle") is False
    assert prog.get("done") is False or prog.get("total", 0) >= 0


def test_progress_idle_before_first_scan(preset_env):
    port, _, _ = preset_env
    url = f"http://127.0.0.1:{port}/api/progress?since=0"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data.get("idle") is True
    assert data.get("done") is True
    assert data.get("works") == []
    assert isinstance(data.get("scan_presets"), list)
    assert len(data["scan_presets"]) == 2
