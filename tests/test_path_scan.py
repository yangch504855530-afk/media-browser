"""MediaScanner 一级枚举：子文件夹作品与根目录平铺媒体（不跑完整扫描线程）。"""
import os

import media_browser as mb


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def test_enumerate_subfolder_with_image(tmp_path, monkeypatch):
    work = tmp_path / "album1"
    work.mkdir()
    (work / "pic.jpg").write_bytes(b"\xff\xd8\xff\xe0")  # 仅占位扩展名供枚举
    monkeypatch.setattr(mb, "get_scan_root", lambda: str(tmp_path.resolve()))
    s = mb.MediaScanner()
    s._enumerate()
    want = _norm(str(work.resolve()))
    assert want in {_norm(p) for p in s.pending_works}
    assert s._pending_root_files == []


def test_enumerate_root_flat_image_only(tmp_path, monkeypatch):
    (tmp_path / "root.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr(mb, "get_scan_root", lambda: str(tmp_path.resolve()))
    s = mb.MediaScanner()
    s._enumerate()
    assert s.pending_works == []
    assert len(s._pending_root_files) == 1


def test_enumerate_skips_trash_name(tmp_path, monkeypatch):
    trash = tmp_path / ".Trash"
    trash.mkdir()
    (trash / "a.jpg").write_bytes(b"x")
    good = tmp_path / "ok"
    good.mkdir()
    (good / "b.jpg").write_bytes(b"x")
    monkeypatch.setattr(mb, "get_scan_root", lambda: str(tmp_path.resolve()))
    s = mb.MediaScanner()
    s._enumerate()
    assert all(".Trash" not in p for p in s.pending_works)
    assert _norm(str(good.resolve())) in {_norm(p) for p in s.pending_works}
