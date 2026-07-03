"""环境变量解析与纯工具函数（不启动 HTTP 服务）。"""
import os

import pytest

import media_browser as mb


def test_int_env_default_and_bounds(monkeypatch):
    monkeypatch.delenv("MB_TEST_INT", raising=False)
    assert mb._int_env("MB_TEST_INT", 3, 1, 10) == 3
    monkeypatch.setenv("MB_TEST_INT", "7")
    assert mb._int_env("MB_TEST_INT", 3, 1, 10) == 7
    monkeypatch.setenv("MB_TEST_INT", "999")
    assert mb._int_env("MB_TEST_INT", 3, 1, 10) == 10
    monkeypatch.setenv("MB_TEST_INT", "-1")
    assert mb._int_env("MB_TEST_INT", 3, 1, 10) == 1
    monkeypatch.setenv("MB_TEST_INT", "notint")
    assert mb._int_env("MB_TEST_INT", 3, 1, 10) == 3


def test_thumbnail_count_defaults_to_eight_even_for_slow_disks(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "_DISK_PROFILE_ENV_SET", True)
    monkeypatch.setattr(mb, "_THUMB_COUNT_ENV_SET", False)
    monkeypatch.setattr(mb, "DISK_PROFILE", "nas")
    monkeypatch.setattr(mb, "THUMB_COUNT", 3)

    mb._apply_perf_profile_for_scan_root(str(tmp_path))

    assert mb.THUMB_COUNT == 8


def test_normalize_scan_path_input():
    assert mb._normalize_scan_path_input("  /tmp/foo  ") == "/tmp/foo"
    s = mb._normalize_scan_path_input("\u201c/path/with/curly\u201d")
    assert s == "/path/with/curly"
    assert "\u201c" not in s
    assert mb._normalize_scan_path_input("\ufeff/tmp/x") == "/tmp/x"


def test_ollama_config_from_env(monkeypatch):
    monkeypatch.setenv("MB_OLLAMA_HOST", "http://example.com:11434/")
    monkeypatch.setenv("MB_OLLAMA_MODEL", "  mymodel  ")
    monkeypatch.setenv("MB_ANALYZE_FRAME_COUNT", "10")
    monkeypatch.setenv("MB_OLLAMA_TIMEOUT", "60")
    host, model, frames, timeout = mb._ollama_config()
    assert host == "http://example.com:11434"
    assert model == "mymodel"
    assert frames == 10
    assert timeout == 60


def test_sha256_str_stable():
    assert mb.sha256_str("same") == mb.sha256_str("same")
    assert len(mb.sha256_str("x")) == 16


def test_video_needs_transcoded_play():
    assert mb.video_needs_transcoded_play("/a/b/file.mts") is True
    assert mb.video_needs_transcoded_play("/a/b/x.mp4") is False


def test_is_path_under_root(monkeypatch, tmp_path):
    root = tmp_path.resolve()
    (root / "sub").mkdir()
    monkeypatch.setattr(mb, "get_scan_root", lambda: str(root))
    assert mb.is_path_under_root(str(root / "sub")) is True
    assert mb.is_path_under_root(str(root)) is True
    assert mb.is_path_under_root("/etc/passwd") is False


def test_tags_to_chinese_sentence():
    assert mb._tags_to_chinese_sentence([]) == ""
    assert mb._tags_to_chinese_sentence(["海浪"]) == "海浪。"
    assert mb._tags_to_chinese_sentence(["a", "b"]) == "a、b。"


def test_ffmpeg_hw_configured(monkeypatch):
    monkeypatch.delenv("MB_FFMPEG_HW", raising=False)
    assert mb._ffmpeg_hw_configured() == "auto"
    monkeypatch.setenv("MB_FFMPEG_HW", "auto")
    mb._invalidate_ffmpeg_hw_cache()
    assert mb._ffmpeg_hw_configured() == "auto"
    monkeypatch.setenv("MB_FFMPEG_HW", "nvenc")
    mb._invalidate_ffmpeg_hw_cache()
    assert mb._ffmpeg_hw_configured() == "nvenc"
    monkeypatch.setenv("MB_FFMPEG_HW", "amf")
    mb._invalidate_ffmpeg_hw_cache()
    assert mb._ffmpeg_hw_configured() == "amf"
    monkeypatch.setenv("MB_FFMPEG_HW", "bogus")
    mb._invalidate_ffmpeg_hw_cache()
    assert mb._ffmpeg_hw_configured() == "auto"


def test_resolve_ffmpeg_hw_off(monkeypatch):
    monkeypatch.setenv("MB_FFMPEG_HW", "off")
    mb._invalidate_ffmpeg_hw_cache()
    hw = mb.resolve_ffmpeg_hw()
    assert hw["configured"] == "off"
    assert hw["active"] == "off"
    assert hw["available"] is False


def test_resolve_ffmpeg_hw_auto_without_device(monkeypatch):
    monkeypatch.setenv("MB_FFMPEG_HW", "auto")
    monkeypatch.setattr(mb.os, "name", "posix")
    monkeypatch.setattr(mb, "_ffmpeg_vaapi_device_path", lambda: None)
    monkeypatch.setattr(mb, "_ffmpeg_has_encoder", lambda _e: True)
    monkeypatch.setattr(mb, "_ffmpeg_encoder_runtime_usable", lambda _mode: False)
    mb._invalidate_ffmpeg_hw_cache()
    hw = mb.resolve_ffmpeg_hw()
    assert hw["active"] == "off"
    assert hw["available"] is False
    assert hw["error"] is None
    assert "details" in hw


def test_resolve_ffmpeg_hw_auto_prefers_windows_qsv(monkeypatch):
    monkeypatch.setenv("MB_FFMPEG_HW", "auto")
    monkeypatch.setattr(mb.os, "name", "nt")
    monkeypatch.setattr(mb, "_ffmpeg_vaapi_device_path", lambda: None)
    monkeypatch.setattr(mb, "_ffmpeg_has_encoder", lambda _e: True)
    monkeypatch.setattr(mb, "_ffmpeg_encoder_runtime_usable", lambda mode: mode == "qsv")
    mb._invalidate_ffmpeg_hw_cache()
    hw = mb.resolve_ffmpeg_hw()
    assert hw["configured"] == "auto"
    assert hw["active"] == "qsv"
    assert hw["available"] is True


def test_ffmpeg_build_transcode_cmd_vaapi():
    cmd = mb._ffmpeg_build_transcode_cmd(
        "/src.avi",
        "/out.mp4",
        has_audio=True,
        hw_mode="vaapi",
        vaapi_device="/dev/dri/renderD128",
        for_pipe=False,
    )
    assert "-vaapi_device" in cmd
    assert "h264_vaapi" in cmd
    assert cmd[-1] == "/out.mp4"


def test_ffmpeg_build_transcode_cmd_nvenc_and_amf():
    nv = mb._ffmpeg_build_transcode_cmd(
        "/src.avi",
        "/out.mp4",
        has_audio=False,
        hw_mode="nvenc",
        vaapi_device=None,
        for_pipe=False,
    )
    assert "h264_nvenc" in nv
    assert "-hwaccel" in nv
    amf = mb._ffmpeg_build_transcode_cmd(
        "/src.avi",
        "pipe:1",
        has_audio=True,
        hw_mode="amf",
        vaapi_device=None,
        for_pipe=True,
    )
    assert "h264_amf" in amf
    assert "pipe:1" == amf[-1]
