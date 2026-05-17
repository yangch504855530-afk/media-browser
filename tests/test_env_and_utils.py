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
