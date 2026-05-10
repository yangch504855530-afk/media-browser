#!/usr/bin/env bash
# 在 macOS 上生成 Media Browser.app 与 DMG（须已安装 ffmpeg/ffprobe）
#
# 重要：请勿在外置盘（exFAT/部分移动硬盘）上直接作为 --distpath 构建，
# 否则易产生 ._ AppleDouble 文件，导致 codesign「Operation not permitted」。
# 本脚本默认把 PyInstaller 产出放在本机 $TMPDIR（一般为启动磁盘），再拷回 dist/。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="1.0.12"
VENV="$ROOT/.venv-build"

# 减少在非 APFS 卷上写出资源分支文件（._xxx）
export COPYFILE_DISABLE=1

echo "==> venv + PyInstaller"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -q --upgrade pip
python -m pip install -q "pyinstaller>=6.0"

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
python -m PyInstaller packaging/MediaBrowser.spec \
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
rsync -a "$DISTDIR/" "$ROOT/dist/"
APP="$ROOT/dist/Media Browser.app"
strip_appledouble "$APP"

DMG_NAME="Media Browser v${VERSION}.dmg"
DMG_TMP="${TMPDIR:-/tmp}/${DMG_NAME}"
DMG_OUT="$ROOT/dist/$DMG_NAME"

echo "==> Creating DMG on boot volume temp, then copy to dist/"
rm -f "$DMG_TMP" "$DMG_OUT"
hdiutil create -volname "Media Browser ${VERSION}" -srcfolder "$APP_SRC" -ov -format UDZO "$DMG_TMP"
cp -f "$DMG_TMP" "$DMG_OUT"
rm -f "$DMG_TMP"

echo "==> Cleanup temp build trees"
rm -rf "$WORKDIR" "$DISTDIR"

echo "Done:"
ls -la "$APP" "$DMG_OUT"
