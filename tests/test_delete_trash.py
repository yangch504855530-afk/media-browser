"""废纸篓队列：仅队列子集删除、路径归一化。"""

import media_browser as mb


def test_delete_trash_delete_selected_subset(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    a = root / "a.bin"
    b = root / "b.bin"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    monkeypatch.setattr(mb, "_scan_root", str(root))

    mb.delete_trash_clear()
    mb.delete_trash_add(str(a), "e1")
    mb.delete_trash_add(str(b), "e2")

    r = mb.delete_trash_delete_selected([str(a)])
    assert r["ok"] is True
    assert r["deleted"] == 1
    assert not a.exists()
    assert b.exists()
    items = mb.delete_trash_list()
    assert len(items) == 1
    assert items[0]["path"] == str(b)


def test_delete_trash_delete_selected_not_in_queue(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "only.bin"
    f.write_bytes(b"x")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    monkeypatch.setattr(mb, "_scan_root", str(root))

    mb.delete_trash_clear()
    r = mb.delete_trash_delete_selected([str(f)])
    assert r["deleted"] == 0
    assert r["skipped"]
    assert any(x.get("error") == "not_in_trash_queue" for x in r["skipped"])
    assert f.exists()


def test_delete_trash_remove_paths_norm(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache))
    monkeypatch.setattr(mb, "_scan_root", str(root))
    p = root / "x.bin"
    p.write_bytes(b"1")
    mb.delete_trash_clear()
    mb.delete_trash_add(str(p), "err")
    n = mb.delete_trash_remove_paths([str(p)])
    assert n == 0
    assert mb.delete_trash_list() == []
