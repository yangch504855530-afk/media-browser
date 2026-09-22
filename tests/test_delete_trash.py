"""Recycle metadata, idempotency and escape protection."""

import json
from pathlib import Path

import media_browser as mb


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    monkeypatch.setattr(mb, "_scan_root", str(root.resolve()))
    mb.reset_recycle_for_tests()
    return root


def test_move_and_repeat_are_idempotent(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    media = root / "nested"
    media.mkdir()
    media = media / "a.bin"
    media.write_bytes(b"a")
    first, already = mb.move_media_to_recycle(str(media))
    assert already is False
    assert first["original_root"] == str(root.resolve())
    assert first["relative_parts"] == ["nested", "a.bin"]
    second, already = mb.move_media_to_recycle(str(media))
    assert already is True
    assert second["id"] == first["id"]
    assert len(mb.delete_trash_list()) == 1


def test_restore_rejects_relative_escape(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    media = root / "a.bin"
    media.write_bytes(b"a")
    entry, _ = mb.move_media_to_recycle(str(media))
    manifest = Path(mb._delete_trash_store_path())
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["items"][0]["relative_parts"] = ["..", "escaped.bin"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    result = mb.restore_recycle_entries([entry["id"]])
    assert result["restored_count"] == 0
    assert result["skipped"][0]["code"] in ("INVALID_RECYCLE_PATH", "PATH_OUTSIDE_ROOT")
    assert not (root.parent / "escaped.bin").exists()
