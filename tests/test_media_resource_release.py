"""Windows media deletion releases active transcodes and retries file locks."""

from __future__ import annotations

import media_browser as mb


class _FakeProc:
    def __init__(self):
        self.killed = 0
        self.waited = 0

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1


def test_release_media_resources_stops_registered_transcode(tmp_path):
    media = tmp_path / "playing.avi"
    media.write_bytes(b"x")
    proc = _FakeProc()

    mb._register_ffmpeg_proc(str(media), proc)
    assert mb.release_media_resources(str(media)) == 1
    assert proc.killed == 1
    assert proc.waited == 1
    assert mb.release_media_resources(str(media)) == 0


def test_safe_remove_retries_windows_permission_error(tmp_path, monkeypatch):
    media = tmp_path / "playing.mp4"
    media.write_bytes(b"x")
    real_remove = mb.os.remove
    attempts = []

    def locked_twice(path):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("file in use")
        real_remove(path)

    monkeypatch.setattr(mb.sys, "platform", "win32")
    monkeypatch.setattr(mb.os, "remove", locked_twice)
    monkeypatch.setattr(mb.time, "sleep", lambda _delay: None)

    mb._safe_remove(str(media))
    assert len(attempts) == 3
    assert not media.exists()


def test_safe_remove_deletes_play_ready_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    media = tmp_path / "playing.avi"
    media.write_bytes(b"x")
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    cached = mb.play_cache_path(str(media))
    mb.os.makedirs(mb.os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as f:
        f.write(b"cached")

    mb._safe_remove(str(media))
    assert not media.exists()
    assert not mb.os.path.exists(cached)


def test_ffmpeg_run_transcode_registers_background_process(monkeypatch):
    events = []

    class Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            events.append("kill")

    proc = Proc()
    monkeypatch.setattr(mb.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(mb, "_register_ffmpeg_proc", lambda source, p: events.append(("add", source, p)))
    monkeypatch.setattr(mb, "_unregister_ffmpeg_proc", lambda source, p: events.append(("del", source, p)))

    mb._ffmpeg_run_transcode(["ffmpeg"], "/media/source.avi")
    assert events == [
        ("add", "/media/source.avi", proc),
        ("del", "/media/source.avi", proc),
    ]


def test_prune_play_cache_removes_oldest_files(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    root = cache / "play_mp4" / "aa"
    root.mkdir(parents=True)
    old = root / "old.mp4"
    new = root / "new.mp4"
    old.write_bytes(b"a" * 10)
    new.write_bytes(b"b" * 10)
    mb.os.utime(old, (1, 1))
    mb.os.utime(new, (2, 2))
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))

    assert mb.prune_play_cache(10) == 1
    assert not old.exists()
    assert new.exists()
