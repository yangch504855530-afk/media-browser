"""Preset 模式首页 HTTP 契约：服务端注入 + 客户端初始化前提（QA 回归）。"""

from __future__ import annotations

import json
import re
import threading
import urllib.request

import pytest

import media_browser as mb


@pytest.fixture()
def preset_http(tmp_path, monkeypatch):
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
    monkeypatch.setenv("MB_SCAN_ROOT_READONLY", "1")
    mb.bootstrap_scan_configuration()
    idle = mb.MediaScanner()
    idle.mark_idle()
    monkeypatch.setattr(mb, "scanner", idle)
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


def _fetch_home(port: int) -> str:
    url = f"http://127.0.0.1:{port}/"
    with urllib.request.urlopen(url, timeout=15) as resp:
        assert resp.status == 200
        return resp.read().decode("utf-8")


def test_get_home_injects_preset_mode_true(preset_http):
    """Docker preset：GET / 必须注入 preset 模式与媒体库列表，否则页内只会是文本框。"""
    port, lib_a, lib_b = preset_http
    html = _fetch_home(port)
    assert "__MB_SCAN_PRESET_MODE__" not in html
    assert "__MB_SCAN_PRESETS_JSON__" not in html
    m_mode = re.search(r"const MB_SCAN_PRESET_MODE = \((true|false)", html)
    assert m_mode, "MB_SCAN_PRESET_MODE assignment missing"
    assert m_mode.group(1) == "true"
    m_presets = re.search(r"const MB_SCAN_PRESETS = (\[.*?\]);", html, re.S)
    assert m_presets, "MB_SCAN_PRESETS assignment missing"
    presets = json.loads(m_presets.group(1))
    assert len(presets) == 2
    paths = {p["path"] for p in presets}
    assert str(lib_a.resolve()) in paths
    assert str(lib_b.resolve()) in paths


def test_get_home_preset_ui_prerequisites(preset_http):
    """preset 下拉切换所需 DOM/JS 与 v1.3.0 一致（mbInitScanPresetUi 依赖）。"""
    port, _, _ = preset_http
    html = _fetch_home(port)
    for marker in (
        'id="scanRootPreset"',
        'id="scanRootInput"',
        'id="scanRootLabel"',
        'id="applyScanRoot"',
        "function mbInitScanPresetUi",
        "function renderMoreWorks(list)",
        'btn.textContent = \'切换并扫描\'',
        "inp.style.display = 'none'",
    ):
        assert marker in html, marker
    assert 'id="scanRootPreset" class="mb-hidden"' in html


def test_health_exposes_presets_when_configured(preset_http):
    port, _, _ = preset_http
    url = f"http://127.0.0.1:{port}/health"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    scan = body.get("scan") or {}
    assert isinstance(scan.get("presets"), list)
    assert len(scan["presets"]) == 2
