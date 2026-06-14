import media_browser as mb


def test_scanner_marks_unreadable_video_as_invalid(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    monkeypatch.setattr(
        mb,
        "get_video_info",
        lambda _path: {
            "duration": 0.0,
            "width": 0,
            "height": 0,
            "codec": "",
            "bitrate": 0,
            "fps": 0.0,
        },
    )
    monkeypatch.setattr(mb, "generate_video_thumb_single", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(mb, "generate_video_thumbs", lambda *_args, **_kwargs: [])

    root = tmp_path / "library"
    work = root / "broken"
    work.mkdir(parents=True)
    video = work / "broken.mp4"
    video.write_bytes(b"not a video")
    assert mb.replace_scan_root(str(root.resolve())) is True

    scanner = mb.MediaScanner()
    try:
        built = scanner._process_work(str(work))
    finally:
        scanner._executor.shutdown(wait=False)

    assert built["invalid_count"] == 1
    assert built["all_invalid"] is True
    assert built["items"][0]["invalid_reason"] == "无法读取视频信息"
