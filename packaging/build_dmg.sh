#!/usr/bin/env bash
# 在 macOS 上生成 Media Browser.app 与 DMG（须已安装 ffmpeg/ffprobe）
#
# 重要：请勿在外置盘（exFAT/部分移动硬盘）上直接作为 --distpath 构建，
# 否则易产生 ._ AppleDouble 文件，导致 codesign「Operation not permitted」。
# 本脚本默认把 PyInstaller 产出放在本机 $TMPDIR（一般为启动磁盘），再拷回 dist/。
set -eo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(python3 -c 'import re, pathlib; t=pathlib.Path("media_browser.py").read_text(encoding="utf-8"); m=re.search(r"APP_VERSION = \"([^\"]+)\"", t); print(m.group(1) if m else "0.0.0")')"

# 减少在非 APFS 卷上写出资源分支文件（._xxx）
export COPYFILE_DISABLE=1

# CI 环境（如 GitHub Actions）通常已有隔离的 Python，跳过 venv
if [[ "${CI:-}" == "true" ]]; then
    echo "==> CI detected, using system Python directly"
    PYTHON="${PYTHON_CMD:-python3}"
else
    VENV="$ROOT/.venv-build"
    echo "==> Create venv + install PyInstaller"
    python3 -m venv "$VENV"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    PYTHON="python"
fi

$PYTHON -m pip install --upgrade pip
$PYTHON -m pip install "pyinstaller>=6.0"

# 工作目录与产出放在启动磁盘临时目录（避免外置卷导致 codesign / hdiutil 失败）
STAMP="$(date +%s)-$$"
WORKDIR="${TMPDIR:-/tmp}/mb-work-${STAMP}"
DISTDIR="${TMPDIR:-/tmp}/mb-dist-${STAMP}"
rm -rf "$WORKDIR" "$DISTDIR"
mkdir -p "$DISTDIR"

echo "==> Building app bundle (workpath/distpath on boot volume)"
echo "    workpath: $WORKDIR"
echo "    distpath: $DISTDIR"
rm -rf "$ROOT/build" "$ROOT/dist"
$PYTHON -m PyInstaller packaging/MediaBrowser.spec \
  --workpath "$WORKDIR" \
  --distpath "$DISTDIR"

APP_SRC="$DISTDIR/Media Browser.app"
if [[ ! -d "$APP_SRC" ]]; then
  echo "Build failed: $APP_SRC not found" >&2
  exit 1
fi

# 清理误入的 AppleDouble，便于手动 codesign / 归档
strip_appledouble() {
  local target="$1"
  find "$target" -name '._*' -print -delete 2>/dev/null || true
  dot_clean -m "$target" 2>/dev/null || true
  xattr -cr "$target" 2>/dev/null || true
}
strip_appledouble "$APP_SRC"

echo "==> Copy .app to project dist/"
rm -rf "$ROOT/dist"
mkdir -p "$ROOT/dist"
cp -R "$DISTDIR/" "$ROOT/dist/"
APP="$ROOT/dist/Media Browser.app"
strip_appledouble "$APP"

DMG_SUFFIX="${MB_DMG_FLAVOR:-}"
if [[ -n "$DMG_SUFFIX" ]]; then
  DMG_NAME="Media Browser v${VERSION}-${DMG_SUFFIX}.dmg"
else
  DMG_NAME="Media Browser v${VERSION}.dmg"
fi
DMG_TMP="${TMPDIR:-/tmp}/${DMG_NAME}"
DMG_OUT="$ROOT/dist/$DMG_NAME"

echo "==> Creating DMG on boot volume temp, then copy to dist/"
rm -f "$DMG_TMP" "$DMG_OUT"
create_dmg_ok=0
for attempt in 1 2 3 4; do
  rm -f "$DMG_TMP"
  if hdiutil create -volname "Media Browser ${VERSION}" -srcfolder "$APP_SRC" -ov -format UDZO "$DMG_TMP"; then
    create_dmg_ok=1
    break
  fi
  echo "hdiutil create failed (attempt ${attempt}/4), retrying..." >&2
  sleep $((attempt * 4))
done
if [[ "$create_dmg_ok" -ne 1 ]]; then
  echo "hdiutil create failed after retries" >&2
  exit 1
fi
cp -f "$DMG_TMP" "$DMG_OUT"
rm -f "$DMG_TMP"

echo "==> Cleanup temp build trees"
rm -rf "$WORKDIR" "$DISTDIR"

echo "Done:"
ls -la "$APP" "$DMG_OUT"
