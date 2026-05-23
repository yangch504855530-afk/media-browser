"""视频播放：/play 转码 vs /file 直出（手机 mp4 须走 /file + Range）。"""

from __future__ import annotations

import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import media_browser as mb


@pytest.fixture()
def play_server(tmp_path, monkeypatch):
    work = tmp_path / "album"
    work.mkdir()
    mp4 = work / "clip.mp4"
    mkv = work / "clip.mkv"
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
            "color=c=black:s=160x90:d=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(mp4),
        ],
        check=True,
        capture_output=True,
    )
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
            "color=c=blue:s=160x90:d=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(mkv),
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
        yield port, mp4, mkv
    finally:
        srv.shutdown()
        srv.server_close()


def test_video_play_needs_transcode_mobile_mp4_is_false():
    assert mb.video_play_needs_transcode("/a/b/c.mp4", mobile=True) is False
    assert mb.video_play_needs_transcode("/a/b/c.mts", mobile=True) is True
    assert mb.video_play_needs_transcode("/a/b/c.mkv", mobile=True) is True
    assert mb.video_play_needs_transcode("/a/b/c.mp4", mobile=False) is False


def test_build_video_play_url_mobile_native_exts_in_html():
    html = mb.HTML_PAGE
    assert "MOBILE_NATIVE_VIDEO_EXTS" in html
    assert "mbMobile && !MOBILE_NATIVE_VIDEO_EXTS.has(ext)" in html


@pytest.mark.ffmpeg
def test_play_accepts_mp4_and_streams(play_server, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    port, mp4, _mkv = play_server
    q = urllib.parse.urlencode({"path": str(mp4)})
    url = f"http://127.0.0.1:{port}/play?{q}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            assert resp.status == 200
            assert "video" in (resp.headers.get("Content-Type") or "")
            assert len(resp.read(4096)) > 0
    except urllib.error.HTTPError as e:
        pytest.fail(f"/play mp4 failed: HTTP {e.code} {e.read()[:200]}")


@pytest.mark.ffmpeg
def test_file_mp4_supports_range(play_server, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    port, mp4, _mkv = play_server
    q = urllib.parse.urlencode({"path": str(mp4)})
    url = f"http://127.0.0.1:{port}/file?{q}"
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 206
        body = resp.read()
        assert len(body) == 100
        assert resp.headers.get("Accept-Ranges") == "bytes"


@pytest.mark.ffmpeg
def test_play_mkv_transcode(play_server, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    port, _mp4, mkv = play_server
    q = urllib.parse.urlencode({"path": str(mkv)})
    url = f"http://127.0.0.1:{port}/play?{q}"
    with urllib.request.urlopen(url, timeout=45) as resp:
        assert resp.status == 200
        assert len(resp.read(8192)) > 0
