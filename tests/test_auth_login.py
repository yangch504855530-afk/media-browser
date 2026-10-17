"""测试账号密码表单登录与会话认证。"""

import json
import socket
import threading
import urllib.request
import urllib.error
import http.cookiejar as cj
import base64
import pytest
import media_browser as mb


@pytest.fixture
def auth_server(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "AUTH_USER", "yangch504855530")
    monkeypatch.setattr(mb, "AUTH_PASSWORD", "!Q2w3e4r5t6y7u")
    monkeypatch.setattr(mb, "ACCESS_TOKEN", "")
    monkeypatch.setattr(mb, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(mb, "_scan_root", str(tmp_path))
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))

    # 动态分配可用端口
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = mb.ThreadedHTTPServer(("127.0.0.1", port), mb.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_unauthenticated_returns_login_page(auth_server):
    req = urllib.request.Request(f"{auth_server}/")
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        html = res.read().decode("utf-8")
        assert "登录" in html
        assert 'name="username"' in html
        assert 'name="password"' in html


def test_login_wrong_credentials(auth_server):
    req = urllib.request.Request(
        f"{auth_server}/api/login",
        data=json.dumps({"username": "yangch504855530", "password": "wrong_password"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401
    body = json.loads(exc_info.value.read().decode("utf-8"))
    assert body["ok"] is False
    assert "账号或密码错误" in body["error"]


def test_login_success_and_session_flow(auth_server):
    jar = cj.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # 1. 登录
    login_req = urllib.request.Request(
        f"{auth_server}/api/login",
        data=json.dumps({"username": "yangch504855530", "password": "!Q2w3e4r5t6y7u", "remember": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(login_req) as res:
        assert res.status == 200
        body = json.loads(res.read().decode("utf-8"))
        assert body["ok"] is True

    # 验证 Cookie 设置
    cookie_names = [c.name for c in jar]
    assert "mb_auth_token" in cookie_names

    # 2. 凭借 Cookie 访问首页：返回主界面 index.html 而非登录页
    with opener.open(f"{auth_server}/") as res:
        assert res.status == 200
        html = res.read().decode("utf-8")
        assert "退出登录" in html
        assert "loginCard" not in html

    # 3. 退出登录
    logout_req = urllib.request.Request(f"{auth_server}/api/logout", method="POST")
    with opener.open(logout_req) as res:
        assert res.status == 200
        body = json.loads(res.read().decode("utf-8"))
        assert body["ok"] is True


def test_basic_auth_compatibility(auth_server):
    creds = base64.b64encode(b"yangch504855530:!Q2w3e4r5t6y7u").decode("ascii")
    req = urllib.request.Request(
        f"{auth_server}/api/progress",
        headers={"Authorization": f"Basic {creds}"},
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        body = json.loads(res.read().decode("utf-8"))
        assert "scanned" in body


def test_form_post_login_and_get_logout(auth_server):
    jar = cj.CookieJar()
    # opener 不自动跟随 303 以便验证重定向和 Set-Cookie
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_303(self, req, fp, code, msg, headers):
            return fp

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
    form_bytes = "username=yangch504855530&password=%21Q2w3e4r5t6y7u&remember=1".encode("utf-8")
    req = urllib.request.Request(
        f"{auth_server}/api/login",
        data=form_bytes,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(req) as res:
        assert res.status == 303
        assert res.headers.get("Location") == "/"
        assert "mb_auth_token" in [c.name for c in jar]

    # GET /api/logout 重定向至 /
    with opener.open(f"{auth_server}/api/logout") as res:
        assert res.status == 303
        assert res.headers.get("Location") == "/"

