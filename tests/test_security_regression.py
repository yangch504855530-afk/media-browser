"""Security regressions for LAN access, request limits, and cache paths."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
import http.cookiejar
import base64
import time
import os
import subprocess
import sys
from pathlib import Path

import pytest

import media_browser as mb


def _serve():
    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _basic_header(username: str, password: str) -> str:
    raw = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {raw}"


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


def test_non_local_host_accepts_original_basic_account(monkeypatch):
    monkeypatch.setattr(mb, "HOST", "0.0.0.0")
    monkeypatch.setattr(mb, "ACCESS_TOKEN", "")
    monkeypatch.setattr(mb, "AUTH_USERNAME", "nas-user")
    monkeypatch.setattr(mb, "AUTH_PASSWORD", "original-password")
    monkeypatch.setattr(mb.scanner, "works", [])
    monkeypatch.setattr(mb.scanner, "done", True)
    srv = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/works"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(url, timeout=10)
        assert missing.value.code == 401
        assert missing.value.headers.get("WWW-Authenticate", "").startswith("Basic ")

        wrong = urllib.request.Request(
            url,
            headers={"Authorization": _basic_header("nas-user", "wrong-password")},
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(wrong, timeout=10)
        assert ei.value.code == 401

        req = urllib.request.Request(
            url,
            headers={"Authorization": _basic_header("nas-user", "original-password")},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["ok"] is True
            assert payload["done"] is True
            assert payload["works"] == []
            assert {"scan_root", "directories", "total"} <= payload.keys()
    finally:
        srv.shutdown()
        srv.server_close()


def test_basic_auth_accepts_mobile_browser_homepage(monkeypatch):
    monkeypatch.setattr(mb, "HOST", "0.0.0.0")
    monkeypatch.setattr(mb, "ACCESS_TOKEN", "")
    monkeypatch.setattr(mb, "AUTH_USERNAME", "nas-user")
    monkeypatch.setattr(mb, "AUTH_PASSWORD", "original-password")
    srv = _serve()
    try:
        base_url = f"http://127.0.0.1:{srv.server_address[1]}/"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base_url, timeout=10)
        assert missing.value.code == 401
        assert missing.value.headers.get("WWW-Authenticate", "").startswith("Basic ")

        req = urllib.request.Request(
            base_url,
            headers={
                "Authorization": _basic_header("nas-user", "original-password"),
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
        assert "<title>Media Browser v" in body
        assert 'name="viewport"' in body
        assert "matchMedia('(max-width: 767px)')" in body
    finally:
        srv.shutdown()
        srv.server_close()


def test_legacy_nas_auth_environment_aliases_are_supported():
    env = os.environ.copy()
    env.update(
        {
            "MB_AUTH_USERNAME": "",
            "MB_BASIC_AUTH_USERNAME": "",
            "MB_AUTH_USER": "legacy-nas-user",
            "MB_AUTH_PASSWORD": "legacy-original-password",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import media_browser as mb; print(mb.AUTH_USERNAME); print(mb.AUTH_PASSWORD)",
        ],
        env=env,
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.splitlines() == ["legacy-nas-user", "legacy-original-password"]


def test_non_local_startup_requires_token_or_complete_basic_auth(monkeypatch):
    assert mb._auth_startup_error(
        host="0.0.0.0", token="", username="", password=""
    ) == (
        "MB_ACCESS_TOKEN or MB_AUTH_USERNAME/MB_AUTH_PASSWORD is required "
        "when MB_HOST is not localhost/127.0.0.1/::1"
    )
    assert mb._auth_startup_error(
        host="0.0.0.0", token="", username="nas-user", password=""
    )
    assert mb._auth_startup_error(
        host="0.0.0.0",
        token="secret-token",
        username="",
        password="",
    ) is None
    assert mb._auth_startup_error(
        host="0.0.0.0",
        token="",
        username="nas-user",
        password="original-password",
    ) is None
    assert mb._auth_startup_error(
        host="127.0.0.1", token="", username="", password=""
    ) is None


def test_basic_auth_allows_media_recycle_and_keeps_payload(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    root = tmp_path / "library"
    album = root / "album"
    album.mkdir(parents=True)
    media = album / "clip.mp4"
    media.write_bytes(b"original-basic-auth-payload")

    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    assert mb.replace_scan_root(str(root.resolve())) is True
    deadline = time.monotonic() + 10
    while not mb.scanner.done and time.monotonic() < deadline:
        time.sleep(0.02)
    mb.reset_recycle_for_tests()

    monkeypatch.setattr(mb, "HOST", "0.0.0.0")
    monkeypatch.setattr(mb, "ACCESS_TOKEN", "")
    monkeypatch.setattr(mb, "AUTH_USERNAME", "nas-user")
    monkeypatch.setattr(mb, "AUTH_PASSWORD", "original-password")
    srv = _serve()
    try:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": _basic_header("nas-user", "original-password"),
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/delete",
            data=json.dumps({"path": str(media.resolve())}).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            deleted = json.loads(resp.read().decode("utf-8"))
        assert deleted["ok"] is True
        assert deleted["already_recycled"] is False
        assert not media.exists()

        list_req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/delete-trash",
            headers=headers,
        )
        with urllib.request.urlopen(list_req, timeout=10) as resp:
            assert resp.status == 200
            recycle = json.loads(resp.read().decode("utf-8"))
        assert recycle["ok"] is True
        assert recycle["count"] == 1
        assert recycle["items"][0]["relative_path"] == "album/clip.mp4"
        object_path = Path(recycle["items"][0]["object_path"])
        assert object_path.read_bytes() == b"original-basic-auth-payload"
    finally:
        safe = tmp_path / "safe-final"
        safe.mkdir(exist_ok=True)
        mb.replace_scan_root(str(safe.resolve()))
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
