"""多媒体库扫描解耦与每日增量调度测试。"""

import os
import time
import json
import threading
from urllib.request import Request, urlopen
import pytest
import media_browser as mb


@pytest.fixture
def multi_lib_env(tmp_path, monkeypatch):
    lib_a = tmp_path / "lib_a"
    lib_b = tmp_path / "lib_b"
    cache_dir = tmp_path / "cache"
    lib_a.mkdir()
    lib_b.mkdir()
    cache_dir.mkdir()

    # Create media items in lib_a
    (lib_a / "work1").mkdir()
    (lib_a / "work1" / "img1.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    (lib_a / "work2").mkdir()
    (lib_a / "work2" / "img2.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    # Create media items in lib_b
    (lib_b / "work3").mkdir()
    (lib_b / "work3" / "img3.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    presets = f"{lib_a}|LibA;{lib_b}|LibB"
    monkeypatch.setenv("MB_SCAN_PRESETS", presets)
    monkeypatch.setenv("MB_ROOT_DIR", str(lib_a))
    monkeypatch.setenv("MB_AUTO_SCAN", "0")
    monkeypatch.setenv("MB_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(mb, "CACHE_DIR", str(cache_dir))

    mb.bootstrap_scan_configuration()
    mb._library_indices.clear()
    mb._library_manager._scanners.clear()

    yield lib_a, lib_b, cache_dir

    mb._library_manager.shutdown_all()


def test_switch_library_does_not_cancel_running_scan(multi_lib_env):
    """切换查看媒体库时，不中断后台正在运行的扫描任务。"""
    lib_a, lib_b, _ = multi_lib_env

    # 启动 lib_a 扫描
    sc_a = mb._library_manager.start_scan(str(lib_a))
    assert sc_a.started

    # 立即切换查看 lib_b
    ok = mb.replace_scan_root(str(lib_b), persist=False, trigger_scan=False)
    assert ok
    assert mb.get_scan_root() == str(lib_b.resolve())

    # 验证 lib_a 的 scanner 没有被杀死，等待完成
    if sc_a.thread and sc_a.thread.is_alive():
        sc_a.thread.join(timeout=10)
    assert sc_a.done
    assert len(sc_a.works) == 2

    # 切换回 lib_a 查看，其 works 已加载完整
    ok = mb.replace_scan_root(str(lib_a), persist=False, trigger_scan=False)
    assert ok
    sc_cur = mb._library_manager.get_scanner(str(lib_a))
    assert sc_cur.done
    assert len(sc_cur.works) == 2


def test_incremental_scan_and_prune_deleted(multi_lib_env):
    """增量扫描复用未修改项签名，并修剪磁盘上已删除的作品。"""
    lib_a, _, _ = multi_lib_env

    # 首次扫描 lib_a
    sc1 = mb._library_manager.start_scan(str(lib_a))
    if sc1.thread and sc1.thread.is_alive():
        sc1.thread.join(timeout=10)
    assert len(sc1.works) == 2
    work_names = {w["name"] for w in sc1.works}
    assert "work1" in work_names
    assert "work2" in work_names

    # 删除 work2
    import shutil
    shutil.rmtree(lib_a / "work2")

    # 再次扫描 lib_a
    sc2 = mb._library_manager.start_scan(str(lib_a), force_restart=True)
    if sc2.thread and sc2.thread.is_alive():
        sc2.thread.join(timeout=10)
    
    # 验证 work2 已被修剪，只剩 work1
    assert len(sc2.works) == 1
    assert sc2.works[0]["name"] == "work1"

    # 验证索引文件也已更新
    idx = mb.load_library_index(str(lib_a))
    assert len(idx) == 1


def test_daily_scan_scheduler_sequential(multi_lib_env):
    """每日调度器串行扫描所有预设媒体库。"""
    lib_a, lib_b, _ = multi_lib_env

    scheduler = mb.DailyScanScheduler()
    scheduler._run_daily_scan()

    assert scheduler.last_run_time is not None
    # 验证两个库都已完成扫描
    sc_a = mb._library_manager.get_scanner(str(lib_a))
    sc_b = mb._library_manager.get_scanner(str(lib_b))
    assert sc_a.done and len(sc_a.works) == 2
    assert sc_b.done and len(sc_b.works) == 1


def test_api_progress_background_scan_info(multi_lib_env):
    """当后台有其它库在扫描时，/api/progress 返回 background_scan 字段。"""
    lib_a, lib_b, _ = multi_lib_env

    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    try:
        # 当前查看 lib_b
        mb.replace_scan_root(str(lib_b), persist=False, trigger_scan=False)
        # 设置 lib_a 为 scanning 状态
        sc_a = mb._library_manager.get_scanner(str(lib_a))
        sc_a.started = True
        sc_a.done = False
        sc_a.scanned_dirs = 5
        sc_a.total_dirs = 20

        # 查询 /api/progress
        url = f"http://127.0.0.1:{port}/api/progress"
        req = Request(url)
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        assert "background_scan" in data
        assert data["background_scan"] is not None
        assert data["background_scan"]["path"] == str(lib_a.resolve())
        assert data["background_scan"]["scanned"] == 5
        assert data["background_scan"]["total"] == 20

        # 清除状态
        sc_a.done = True
    finally:
        srv.shutdown()
        srv.server_close()
