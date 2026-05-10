# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：在项目根目录执行
#   pip install pyinstaller>=6.0
#   pyinstaller packaging/MediaBrowser.spec

import sys
import os
import shutil

block_cipher = None

SPECDIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(SPECDIR)

ff = shutil.which("ffmpeg")
fp = shutil.which("ffprobe")
binaries = []
for path in (ff, fp):
    if path and os.path.isfile(path):
        binaries.append((path, "."))

a = Analysis(
    [os.path.join(ROOT, "media_browser.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Windows 无控制台窗口；macOS/Linux 保留控制台便于调试
console = sys.platform != "win32"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaBrowser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Media Browser",
)

# BUNDLE 仅在 macOS 上执行
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Media Browser.app",
        icon=None,
        bundle_identifier="com.mediabrowser.app",
        info_plist={
            "CFBundleName": "Media Browser",
            "CFBundleDisplayName": "Media Browser",
            "CFBundleVersion": "1.0.13",
            "CFBundleShortVersionString": "1.0.13",
            "NSHighResolutionCapable": True,
        },
    )
