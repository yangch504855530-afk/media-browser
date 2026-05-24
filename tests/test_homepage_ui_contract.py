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


def test_review_filter_kept_and_pending_labels(homepage_html: str):
    """v1.4.1：筛选区待审阅/已审阅；卡片仍用待审/保留文案。"""
    assert 'data-filter="pending"' in homepage_html
    assert 'data-filter="kept"' in homepage_html
    assert ">待审阅</button>" in homepage_html
    assert ">已审阅</button>" in homepage_html
    assert "if (ft === 'kept') return getWorkReviewTag(w.id) === 'kept';" in homepage_html
    js = _extract_main_script(homepage_html)
    assert "const txt = kept ? '保留' : '待审';" in js
    assert 'id="mbGalReview"' in homepage_html
    assert ">保留</button>" in homepage_html


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
