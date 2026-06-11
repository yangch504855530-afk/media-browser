"""Security regressions for LAN access, request limits, and cache paths."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
import http.cookiejar

import pytest

import media_browser as mb


def _serve():
    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_default_host_is_local_only():
    assert mb.HOST == "127.0.0.1"
    assert mb._host_requires_access_token("127.0.0.1") is False
    assert mb._host_requires_access_token("0.0.0.0") is True


def test_non_local_host_requires_token_for_api(monkeypatch):
    monkeypatch.setattr(mb, "HOST", "0.0.0.0")
    monkeypatch.setattr(mb, "ACCESS_TOKEN", "secret-token")
    srv = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/works"
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(url, timeout=10)
        assert ei.value.code == 401

        req = urllib.request.Request(
            url,
            headers={"Authorization": "Bearer secret-token"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_explicit_token_protects_local_bind(monkeypatch):
    monkeypatch.setattr(mb, "HOST", "127.0.0.1")
    monkeypatch.setattr(mb, "ACCESS_TOKEN", "secret-token")
    srv = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/works"
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(url, timeout=10)
        assert ei.value.code == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_browser_token_login_sets_cookie(monkeypatch):
    monkeypatch.setattr(mb, "HOST", "0.0.0.0")
    monkeypatch.setattr(mb, "ACCESS_TOKEN", "secret-token")
    srv = _serve()
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with opener.open(
            f"http://127.0.0.1:{srv.server_address[1]}/?token=secret-token",
            timeout=10,
        ) as resp:
            assert resp.status == 200
            assert b"Media Browser" in resp.read()
        assert any(cookie.name == "mb_access_token" for cookie in jar)
    finally:
        srv.shutdown()
        srv.server_close()


def test_thumbnail_path_traversal_rejected(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (tmp_path / "secret.txt").write_text("LEAKED", encoding="utf-8")
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    srv = _serve()
    try:
        sock = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        sock.sendall(
            b"GET /thumb/../secret.txt HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        raw = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        assert b"200 OK" not in raw
        assert b"LEAKED" not in raw
    finally:
        srv.shutdown()
        srv.server_close()


def test_oversized_json_body_rejected(monkeypatch):
    monkeypatch.setattr(mb, "MAX_BODY_BYTES", 16)
    srv = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/delete"
        req = urllib.request.Request(
            url,
            data=json.dumps({"path": "x" * 100}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 413
    finally:
        srv.shutdown()
        srv.server_close()


def test_cross_origin_preflight_disabled():
    srv = _serve()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/delete",
            method="OPTIONS",
            headers={"Origin": "https://evil.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_local_mode_rejects_dns_rebinding_host():
    srv = _serve()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/works",
            headers={"Host": "evil.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_mutating_request_rejects_cross_origin(tmp_path):
    srv = _serve()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/delete",
            data=json.dumps({"path": str(tmp_path / "x")}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()
