"""GET /api/play-ready — avi 等格式异步转码缓存。"""

from __future__ import annotations

import subprocess
import threading
import time
import urllib.parse
import urllib.request

import pytest

import media_browser as mb


def test_play_ready_mp4_direct_by_default_but_can_force_transcode(tmp_path, monkeypatch):
    root = tmp_path / "root"
    work = root / "album"
    work.mkdir(parents=True)
    mp4 = work / "clip.mp4"
    mp4.write_bytes(b"fake mp4 body")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    old_root = mb.get_scan_root()
    try:
        assert mb.replace_scan_root(str(root.resolve())) is True
        direct = mb.play_ready_payload(str(mp4))
        assert direct["ok"] is True
        assert direct["ready"] is True
        assert direct["url"].startswith("/file?path=")

        monkeypatch.setattr(mb, "_tool_version_ok", lambda _bin: (False, "forced missing"))
        forced = mb.play_ready_payload(str(mp4), force_transcode=True)
        assert forced["ok"] is False
        assert "ffmpeg" in forced["error"].lower()
    finally:
        mb.replace_scan_root(old_root)


@pytest.fixture()
def avi_server(tmp_path, monkeypatch):
    work = tmp_path / "album"
    work.mkdir()
    avi = work / "clip.avi"
    subprocess.run(
        [
            mb.FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:d=0.5",
            "-c:v",
            "mpeg4",
            str(avi),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield port, avi
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.ffmpeg
def test_play_ready_avi_eventually_ready(avi_server, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    port, avi = avi_server
    q = urllib.parse.urlencode({"path": str(avi)})
    url = f"http://127.0.0.1:{port}/api/play-ready?{q}"
    deadline = time.monotonic() + 90.0
    last = None
    while time.monotonic() < deadline:
        with urllib.request.urlopen(url, timeout=15) as resp:
            last = __import__("json").loads(resp.read().decode("utf-8"))
        if last.get("ready") and last.get("url"):
            assert last["url"].startswith("/file?path=")
            cache_path = urllib.parse.unquote(last["url"].split("path=", 1)[1])
            assert mb.is_play_cache_file(cache_path)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{last['url']}", timeout=15
            ) as fresp:
                assert fresp.status == 200
                assert len(fresp.read(4096)) > 0
            return
        if last.get("status") == "error":
            pytest.fail(last.get("error") or "play-ready error")
        time.sleep(0.5)
    pytest.fail(f"play-ready timeout, last={last}")
