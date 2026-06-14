"""QA E2E：画廊删除成功/失败后自动切换下一文件（Playwright + 嵌入式服务）。"""

from __future__ import annotations

import re
import stat
import sys

import pytest

pytestmark = pytest.mark.e2e


def _delete_mod_key() -> str:
    return "Meta" if sys.platform == "darwin" else "Control"


def _wait_scan_and_open_gallery(page, base_url: str) -> None:
    """等待扫描数据就绪并打开画廊（不依赖 .work-card，避免筛选/虚拟列表导致不可见）。"""
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate(
        """() => {
            localStorage.removeItem('mb_filter');
            localStorage.removeItem('mb_search');
            if (typeof filterType !== 'undefined') filterType = 'all';
            if (typeof searchInput !== 'undefined') searchInput.value = '';
        }"""
    )
    page.wait_for_function(
        """async () => {
            if (typeof allWorks === 'undefined') return false;
            if (allWorks.length > 0 && allWorks[0].items && allWorks[0].items.length >= 2) {
                return true;
            }
            try {
                const r = await fetch('/api/works');
                const d = await r.json();
                if (d.done && d.works && d.works.length > 0) {
                    for (const w of d.works) {
                        if (!allWorks.find(x => x.id === w.id)) allWorks.push(w);
                    }
                    if (typeof scanPollDone !== 'undefined') scanPollDone = true;
                    if (typeof sortAndRenderAll === 'function') sortAndRenderAll();
                    return allWorks.length > 0
                        && allWorks[0].items
                        && allWorks[0].items.length >= 2;
                }
            } catch (e) { /* retry via poll */ }
            return false;
        }""",
        timeout=90_000,
        polling=500,
    )
    page.evaluate("openGallery(allWorks[0].id, 0, -1)")
    page.wait_for_selector("#modal.active", timeout=15_000)


def _current_file_name(page) -> str:
    text = page.locator("#modalFileInfo").inner_text()
    m = re.search(r"([^\s·]+)\.(jpg|jpeg|png|gif|webp)", text, re.I)
    assert m, f"cannot parse file name from modal info: {text!r}"
    return m.group(1).lower()


def test_gallery_switch_releases_previous_video(playwright_browser, gallery_e2e_server):
    """Switching videos pauses and clears the previous media element."""
    base_url, _ = gallery_e2e_server
    page = playwright_browser.new_page()
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof allWorks !== 'undefined' && allWorks.length > 0",
            timeout=90_000,
        )
        result = page.evaluate(
            """() => {
                const work = allWorks[0];
                work.items = [
                    { type: 'video', path: 'first.mp4', name: 'first.mp4', size: 1 },
                    { type: 'video', path: 'second.mp4', name: 'second.mp4', size: 1 },
                ];
                openGallery(work.id, 0, -1, { forceItemIdx: true });
                const oldVideo = document.getElementById('galleryVideo');
                let pauseCalls = 0;
                oldVideo.pause = () => { pauseCalls++; };
                jumpToItem(1);
                return {
                    pauseCalls,
                    oldSrc: oldVideo.getAttribute('src'),
                    oldConnected: oldVideo.isConnected,
                    currentSrc: document.getElementById('galleryVideo')?.getAttribute('src'),
                };
            }"""
        )
        assert result["pauseCalls"] == 1
        assert result["oldSrc"] is None
        assert result["oldConnected"] is False
        assert "second.mp4" in result["currentSrc"]
    finally:
        page.close()


def test_gallery_delete_success_advances_to_next_file(playwright_browser, gallery_e2e_server):
    """删除当前项成功后，画廊应显示同作品下一文件（b）。"""
    base_url, album = gallery_e2e_server
    page = playwright_browser.new_page()
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    try:
        _wait_scan_and_open_gallery(page, base_url)
        assert _current_file_name(page) == "a"
        page.keyboard.press(f"{_delete_mod_key()}+I")
        page.wait_for_function(
            "() => { const t = document.getElementById('modalFileInfo')?.innerText || '';"
            " return t.includes('b.jpg') || t.includes('b.JPG'); }",
            timeout=15_000,
        )
        assert _current_file_name(page) == "b"
        assert not (album / "a.jpg").exists()
        assert (album / "b.jpg").exists()
        assert dialogs == []
    finally:
        page.close()


def test_gallery_delete_last_file_opens_next_work(
    playwright_browser, gallery_two_work_e2e_server
):
    """Deleting the last file in a work continues into the next work."""
    base_url, first, second = gallery_two_work_e2e_server
    page = playwright_browser.new_page()
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof allWorks !== 'undefined' && allWorks.length === 2",
            timeout=90_000,
        )
        expected = page.evaluate(
            """() => {
                const ordered = getFilteredSortedWorks();
                openGallery(ordered[0].id, 0, -1, { forceItemIdx: true });
                return {
                    current: ordered[0].items[0].name,
                    next: ordered[1].items[0].name,
                };
            }"""
        )
        assert expected["current"] in ("first.jpg", "second.jpg")
        page.keyboard.press(f"{_delete_mod_key()}+I")
        page.wait_for_function(
            "(name) => (document.getElementById('modalFileInfo')?.innerText || '').includes(name)",
            arg=expected["next"],
            timeout=15_000,
        )
        assert page.locator("#modal").evaluate("el => el.classList.contains('active')")
        deleted = first / "first.jpg" if expected["current"] == "first.jpg" else second / "second.jpg"
        remaining = second / "second.jpg" if expected["next"] == "second.jpg" else first / "first.jpg"
        assert not deleted.exists()
        assert remaining.exists()
    finally:
        page.close()


def test_gallery_delete_current_work_opens_next_work(
    playwright_browser, gallery_two_work_e2e_server
):
    """Deleting the current work continues into the next work."""
    base_url, _, _ = gallery_two_work_e2e_server
    page = playwright_browser.new_page()
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof allWorks !== 'undefined' && allWorks.length === 2",
            timeout=90_000,
        )
        next_name = page.evaluate(
            """() => {
                const ordered = getFilteredSortedWorks();
                openGallery(ordered[0].id, 0, -1, { forceItemIdx: true });
                return ordered[1].items[0].name;
            }"""
        )
        page.keyboard.press(f"{_delete_mod_key()}+Shift+I")
        page.wait_for_function(
            "(name) => (document.getElementById('modalFileInfo')?.innerText || '').includes(name)",
            arg=next_name,
            timeout=15_000,
        )
        assert page.locator("#modal").evaluate("el => el.classList.contains('active')")
    finally:
        page.close()


def _album_readonly(album) -> None:
    album.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


def _album_writable(album) -> None:
    album.chmod(
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )


def test_gallery_delete_failure_advances_to_next_file(playwright_browser, gallery_e2e_server):
    """删除 API 失败（真实权限错误）时：alert 后仍切到下一文件，且废纸篓角标更新。"""
    base_url, album = gallery_e2e_server
    page = playwright_browser.new_page()
    dialogs: list[str] = []

    def on_dialog(d):
        dialogs.append(d.message)
        d.accept()

    page.on("dialog", on_dialog)
    try:
        _wait_scan_and_open_gallery(page, base_url)
        assert _current_file_name(page) == "a"
        _album_readonly(album)
        page.keyboard.press(f"{_delete_mod_key()}+I")
        page.wait_for_function(
            "() => { const t = document.getElementById('modalFileInfo')?.innerText || '';"
            " return t.includes('b.jpg') || t.includes('b.JPG'); }",
            timeout=15_000,
        )
        assert _current_file_name(page) == "b"
        assert (album / "a.jpg").exists()
        assert (album / "b.jpg").exists()
        page.wait_for_function(
            "() => (document.getElementById('mbTrashCount')?.textContent || '0') !== '0'",
            timeout=15_000,
        )
        assert any("删除失败" in m for m in dialogs)
    finally:
        _album_writable(album)
        page.close()


def test_gallery_delete_work_shortcut_removes_all_media_and_folder(
    playwright_browser, gallery_e2e_server
):
    """⌘⇧I / Ctrl+Shift+I：删除本作品全部媒体并移除已空文件夹（v1.2.3）。"""
    base_url, album = gallery_e2e_server
    page = playwright_browser.new_page()
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    try:
        _wait_scan_and_open_gallery(page, base_url)
        mod = _delete_mod_key()
        page.keyboard.press(f"{mod}+Shift+I")
        page.wait_for_function(
            "() => !document.getElementById('modal')?.classList.contains('active')",
            timeout=20_000,
        )
        page.wait_for_function(
            "() => typeof allWorks !== 'undefined' && allWorks.length === 0",
            timeout=15_000,
        )
        assert not album.exists()
        assert not (album / "a.jpg").exists()
        assert not (album / "b.jpg").exists()
        assert dialogs == []
    finally:
        page.close()


def test_gallery_trash_panel_delete_selected(playwright_browser, gallery_e2e_server):
    """废纸篓：真实失败入队后，勾选「删除所选」可删盘并清空角标。"""
    base_url, album = gallery_e2e_server
    page = playwright_browser.new_page()
    page.on("dialog", lambda d: d.accept())
    try:
        _wait_scan_and_open_gallery(page, base_url)
        _album_readonly(album)
        page.keyboard.press(f"{_delete_mod_key()}+I")
        page.wait_for_function(
            "() => (document.getElementById('mbTrashCount')?.textContent || '0') !== '0'",
            timeout=15_000,
        )
        _album_writable(album)
        page.evaluate("closeModal()")
        page.wait_for_function(
            "() => !document.getElementById('modal')?.classList.contains('active')",
            timeout=5_000,
        )
        page.locator("#mbTrashBtn").click()
        page.wait_for_selector("#mbTrashOverlay.active", timeout=5_000)
        page.locator(".mb-trash-row-chk").first.check()
        page.on("dialog", lambda d: d.accept())
        page.locator("#mbTrashDeleteSelected").click()
        page.wait_for_function(
            "() => document.getElementById('mbTrashCount')?.textContent === '0'",
            timeout=20_000,
        )
        assert not (album / "a.jpg").exists()
    finally:
        _album_writable(album)
        page.close()
