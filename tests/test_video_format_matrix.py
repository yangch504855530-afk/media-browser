"""各视频扩展名的播放策略矩阵（QA 回归）。"""

from __future__ import annotations

import media_browser as mb


def test_transcode_exts_always_use_play_on_mobile_and_desktop():
    for ext in (".avi", ".ts", ".mts", ".wmv", ".flv", ".vob", ".m2ts"):
        assert mb.video_should_use_play_endpoint(f"/x/a{ext}", mobile=True)
        assert mb.video_should_use_play_endpoint(f"/x/a{ext}", mobile=False)


def test_mobile_native_mp4_uses_file_only():
    assert mb.video_should_use_play_endpoint("/a/b/c.mp4", mobile=True) is False
    assert mb.video_should_use_play_endpoint("/a/b/c.mp4", mobile=False) is False


def test_mp4_prefers_direct_even_for_browser_unfriendly_codecs():
    for codec in ("hevc", "h265", "hvc1", "hev1", "mpeg4", "prores", "av1"):
        assert mb.video_codec_needs_transcoded_play("/a/b/c.mp4", codec=codec) is True
        assert mb.video_should_use_play_endpoint("/a/b/c.mp4", codec=codec) is False
        assert mb.video_should_use_play_endpoint("/a/b/c.mov", codec=codec) is False
    for codec in ("h264", "avc1", "AVC"):
        assert mb.video_codec_needs_transcoded_play("/a/b/c.mp4", codec=codec) is False
        assert mb.video_should_use_play_endpoint("/a/b/c.mp4", codec=codec) is False


def test_mobile_mkv_webm_need_transcode():
    for ext in (".mkv", ".webm", ".mpg", ".mpeg"):
        assert ext in mb.VIDEO_EXTS
        assert mb.video_should_use_play_endpoint(f"/z/f{ext}", mobile=True) is True


def test_all_video_exts_classified():
    native = mb.MOBILE_NATIVE_PLAY_EXTS
    transcode = mb.PLAY_TRANSCODE_EXTS
    for ext in mb.VIDEO_EXTS:
        mobile_play = mb.video_should_use_play_endpoint(f"/f/x{ext}", mobile=True)
        desktop_play = mb.video_should_use_play_endpoint(f"/f/x{ext}", mobile=False)
        if ext in transcode:
            assert mobile_play and desktop_play
        elif ext in native:
            assert not mobile_play and not desktop_play
        else:
            assert mobile_play and not desktop_play
