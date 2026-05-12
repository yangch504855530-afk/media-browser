"""缩略图生成（依赖 ffmpeg；无则跳过）。"""
import os
import subprocess

import pytest

import media_browser as mb


@pytest.mark.ffmpeg
def test_generate_image_thumb_writes_jpeg(tmp_path, monkeypatch, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    src = tmp_path / "in.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=cyan:s=64x48",
            "-frames:v",
            "1",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out = mb.generate_image_thumb(str(src))
    assert out.endswith("0.jpg")
    assert os.path.isfile(out)
    assert os.path.getsize(out) > 100


@pytest.mark.ffmpeg
def test_generate_video_thumb_single(tmp_path, monkeypatch, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    vid = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=160x120:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(vid),
        ],
        check=True,
        capture_output=True,
    )
    info = mb.get_video_info(str(vid))
    out = mb.generate_video_thumb_single(str(vid), info)
    assert out.endswith("0.jpg")
    assert os.path.isfile(out)


@pytest.mark.ffmpeg
def test_remove_media_thumb_cache(tmp_path, monkeypatch, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    src = tmp_path / "z.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x32",
            "-frames:v",
            "1",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    mb.generate_image_thumb(str(src))
    key = mb.sha256_str(os.path.abspath(str(src)))
    thumb_dir = os.path.join(str(cache), key)
    assert os.path.isdir(thumb_dir)
    mb.remove_media_thumb_cache(str(src))
    assert not os.path.isdir(thumb_dir)
