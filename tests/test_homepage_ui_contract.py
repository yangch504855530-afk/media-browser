"""首页嵌入式 HTML/JS 契约回归：扫描根 UI、审阅筛选、关键函数完整性。"""

from __future__ import annotations

import html as html_mod
import json
import re
import subprocess
import tempfile

import pytest

import media_browser as mb


def _build_homepage_text() -> str:
    presets_json = json.dumps(mb.get_scan_presets(), ensure_ascii=False)
    return (
        mb.HTML_PAGE.replace("__MB_ROOT_DIR__", html_mod.escape(mb.get_scan_root()))
        .replace("__MB_CACHE_DIR__", html_mod.escape(mb.CACHE_DIR))
        .replace("__MB_CACHE_DIR_JSON__", json.dumps(mb.CACHE_DIR, ensure_ascii=False))
        .replace("__THUMB_COUNT__", str(mb.THUMB_COUNT))
        .replace("__APP_VERSION__", html_mod.escape(mb.APP_VERSION))
        .replace("__MB_SCAN_READONLY__", "true" if mb.scan_root_readonly() else "false")
        .replace(
            "__MB_SCAN_PRESET_MODE__",
            "true" if mb.scan_presets_enabled() else "false",
        )
        .replace("__MB_SCAN_PRESETS_JSON__", presets_json)
    )


def _extract_main_script(html: str) -> str:
    m = re.search(r"<script>\s*(.*?)\s*</script>\s*</body>", html, re.S)
    assert m, "embedded script block missing"
    return m.group(1)


@pytest.fixture()
def homepage_html() -> str:
    return _build_homepage_text()


def test_scan_root_dom_markers_unchanged(homepage_html: str):
    """页眉扫描根结构：文本框、预设下拉、应用按钮（与 v1.3 契约一致）。"""
    for marker in (
        'id="scanRootRow"',
        'id="scanRootInput"',
        'id="scanRootPreset"',
        'id="applyScanRoot"',
        'id="scanRootLabel"',
        'id="mbDrawerScanRootBlock"',
        'id="scanRootPresetDrawer"',
        'id="applyScanRootDrawer"',
    ):
        assert marker in homepage_html, marker


def test_cache_dir_ui_contract(homepage_html: str):
    js = _extract_main_script(homepage_html)
    for marker in (
        'id="cacheDirRow"',
        'id="cacheDirInput"',
        'id="applyCacheDir"',
        'id="cacheDirStatus"',
        "__MB_CACHE_DIR__",
    ):
        if marker == "__MB_CACHE_DIR__":
            assert marker not in homepage_html
        else:
            assert marker in homepage_html, marker
    assert "let MB_CACHE_DIR =" in js
    assert "function mbApplyCacheDirSwitch()" in js
    assert "fetch('/api/set-cache-dir'" in js
    assert "function mbRestartScanUi(statusText)" in js
    assert "mbRestartScanUi('缓存目录已切换" in js


def test_review_filter_kept_and_pending_labels(homepage_html: str):
    """v1.4.1：筛选区待审阅/已审阅；卡片仍用待审/保留文案。"""
    assert 'data-filter="pending"' in homepage_html
    assert 'data-filter="kept"' in homepage_html
    assert 'data-filter="deleted"' in homepage_html
    assert 'data-filter="invalid"' in homepage_html
    assert ">待审阅</button>" in homepage_html
    assert ">已审阅</button>" in homepage_html
    assert "if (ft === 'kept') return getWorkReviewTag(w.id) === 'kept';" in homepage_html
    assert "if (ft === 'deleted') return getWorkReviewTag(w.id) === 'deleted';" in homepage_html
    js = _extract_main_script(homepage_html)
    assert "tag === 'deleted' ? '待删除' : '待定'" in js
    assert 'id="mbGalReview"' in homepage_html
    assert 'id="mbGalPending"' in homepage_html
    assert 'id="mbGalQueueDelete"' in homepage_html
    assert ">保留</button>" in homepage_html


def test_quick_review_is_deferred_and_advances(homepage_html: str):
    js = _extract_main_script(homepage_html)
    assert "function quickReviewCurrentWork(tag)" in js
    assert "quickReviewCurrentWork(e.key === '1' ? 'kept'" in js
    assert "quickReviewCurrentWork('deleted')" in js
    assert "quickReviewCurrentWork('kept');" in js
    assert "(e.key === 'l' || e.key === 'L')" in js
    assert "e.metaKey || e.ctrlKey" in js
    assert "function deleteAllMarkedWorks()" in js
    assert "markWorkOpenedKept(workId);" not in js


def test_embedded_js_critical_functions_present(homepage_html: str):
    """防止误删函数声明导致整页脚本解析失败（曾误删 renderMoreWorks）。"""
    js = _extract_main_script(homepage_html)
    for name in (
        "function sortAndRenderAll",
        "function renderMoreWorks(list)",
        "function mbInitScanPresetUi",
        "function mbApplyScanRootSwitch",
        "function mbGetScanRootSwitchPath",
        "function workMatchesFilter",
        "function mbLoadReviewState",
    ):
        assert name in js, name


def test_embedded_js_parseable_with_node(homepage_html: str):
    """Node 语法检查：脚本块必须可解析，否则扫描根 preset 等初始化不会执行。"""
    js = _extract_main_script(homepage_html)
    try:
        subprocess.run(
            ["node", "--check"],
            input=js,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        pytest.skip("node not installed")
    except subprocess.CalledProcessError as e:
        raise AssertionError(
            (e.stderr or e.stdout or "JS syntax check failed").strip()
        ) from e


def test_gallery_switch_releases_and_invalidates_previous_video(homepage_html: str):
    """Switching gallery items must stop old playback and ignore stale async results."""
    js = _extract_main_script(homepage_html)
    assert "const videoRenderToken = releaseGalleryVideoElement();" in js
    assert "galleryVideoRenderToken++;" in js
    assert "renderToken !== galleryVideoRenderToken || !videoEl.isConnected" in js
    assert "videoRenderToken !== galleryVideoRenderToken || !v.isConnected" in js


def test_gallery_video_mp4_direct_first_with_fallback(homepage_html: str):
    """MP4 containers direct-play first, then force transcode only if playback fails."""
    js = _extract_main_script(homepage_html)
    assert "const MP4_NATIVE_VIDEO_CODECS = new Set(['h264','avc1']);" in js
    assert "function videoCodecNeedsTranscodePlay(filePath, codec)" in js
    assert "function videoItemNeedsTranscodePlay(item)" in js
    assert "return videoNeedsTranscodePlay(item.path || '');" in js
    assert "function fallbackGalleryVideoToTranscode(filePath, videoEl, overlayEl, renderToken, reason)" in js
    assert "function watchDirectVideoFrames(item, videoEl, overlayEl, renderToken, onFallback)" in js
    assert "force=1" in js
    assert "buildVideoPlayUrl(item.path, item)" in js


def test_gallery_delete_shortcuts_skip_confirmation(homepage_html: str):
    """Keyboard delete shortcuts are intentional actions and execute immediately."""
    js = _extract_main_script(homepage_html)
    assert "deleteCurrentWork(true);" in js
    assert "deleteCurrentItem(true);" in js
    assert "if (!skipConfirm && !confirm(" in js


def test_empty_gallery_work_continues_to_next_work(homepage_html: str):
    """An emptied work keeps the gallery open by moving to the next work."""
    js = _extract_main_script(homepage_html)
    assert "function nextGalleryWorkBeforeRemoval(workId)" in js
    assert "function removeEmptyWorkAndContinue(work, nextWork)" in js
    assert "openGallery(nextWork.id, 0, -1, { forceItemIdx: true });" in js


def test_image_grid_contract(homepage_html: str):
    """Image viewing renders grouped grids and can delete the visible group."""
    js = _extract_main_script(homepage_html)
    assert "let imageGridMode = false;" in js
    assert "mb_image_grid_size" in js
    assert "function renderImageGrid(mediaDiv, work)" in js
    assert "data-image-grid-size=\"6\"" in js
    assert "function deleteCurrentImageGridGroup(skipConfirm)" in js
    assert "function navigateImageGrid(work, dir)" in js
    assert "const nextStart = start + (dir > 0 ? imageGridSize : -imageGridSize);" in js
    assert 'class="image-grid size-' in js


def test_gallery_media_stage_uses_available_space(homepage_html: str):
    """Gallery media should fill the left viewing stage instead of being capped to a small viewport."""
    assert "--mb-gallery-stage-maxw: calc(100vw - var(--mb-gallery-sidebar)" in homepage_html
    assert "--mb-gallery-stage-maxh: calc(100vh - 86px);" in homepage_html
    assert "#modalMedia {" in homepage_html
    assert "flex: 1;" in homepage_html
    assert ".modal-main #galleryVideoWrap video" in homepage_html
    assert "object-fit: contain;" in homepage_html
    assert ".image-grid-wrap" in homepage_html
    assert "height: 100%;" in homepage_html


def test_preset_mode_template_vars_when_presets_configured(
    monkeypatch, tmp_path, homepage_html: str
):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setenv("MB_SCAN_PRESETS", f"{lib}|测试库")
    monkeypatch.setenv("MB_ROOT_DIR", str(lib))
    mb.bootstrap_scan_configuration()
    text = _build_homepage_text()
    assert "__MB_SCAN_PRESET_MODE__" not in text
    assert "true" in text or "false" in text
    assert "function mbInitScanPresetUi" in text
    assert 'id="scanRootPreset"' in text
