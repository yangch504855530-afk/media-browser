"""GET /health 负载构建逻辑（不启动 HTTP 服务）。"""

import media_browser as mb


def test_build_health_payload_has_expected_keys():
    body, code = mb.build_health_payload()
    assert code in (200, 503)
    assert "ok" in body
    assert "version" in body
    assert "uptime_seconds" in body
    for k in ("disk", "scan", "ffmpeg", "ffprobe", "ollama", "cache"):
        assert k in body
    assert "hw" in body["ffmpeg"]
    assert body["scan"].get("state") in ("idle", "scanning", "error", "awaiting_scan")
    assert "used_percent" in body["disk"] or "error" in body["disk"]


def test_build_health_503_when_tools_unavailable(monkeypatch):
    monkeypatch.setattr(mb, "_tool_version_ok", lambda _p: (False, "mocked failure"))
    body, code = mb.build_health_payload()
    assert code == 503
    assert body["ok"] is False


def test_build_health_503_on_enum_error(monkeypatch):
    monkeypatch.setattr(mb.scanner, "enum_error", "mock enum failure")
    body, code = mb.build_health_payload()
    assert code == 503
    assert body["scan"]["state"] == "error"
