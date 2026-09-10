#!/usr/bin/env python3
"""
Media Browser - 本地外置硬盘视频/图片流式扫描浏览器
单文件应用，零 Python 依赖（仅需系统 ffmpeg/ffprobe）
用法: python3 media_browser.py
然后在浏览器打开 http://localhost:8765

打包为 macOS .app 时：默认扫描目录为 ~/Documents/MediaBrowser，缓存为
~/Library/Application Support/Media Browser/thumbs；可仍用环境变量覆盖。

环境变量:
  MB_ROOT_DIR  扫描根目录（脚本默认 /Volumes/Untitled/pri；打包 app 默认 ~/Documents/MediaBrowser）。页眉可改路径并点「应用并扫描」
  MB_CACHE_DIR 缩略图/播放转码/审阅账本等缓存目录
  MB_PORT      端口（默认 8765）
  MB_HOST      监听地址（默认 127.0.0.1；局域网访问可设 0.0.0.0，并须设置 MB_ACCESS_TOKEN）
  MB_ACCESS_TOKEN 局域网访问令牌；绑定非本机地址时必填
  MB_MAX_BODY_BYTES JSON 请求体上限字节数（默认 1048576）
  MB_AUTO_OPEN 是否启动后自动打开浏览器（打包默认为是；脚本默认为否，设为 1 可开启）
  MB_SCAN_WORKERS   同时处理「作品」任务的线程数（默认 2；机械盘/NAS 建议 1～2）
  MB_THUMB_COUNT    每个视频条带缩略图帧数（默认 8；越大越慢、越伤盘）
  MB_DISK_PROFILE   设为 slow / nas / hdd / mechanical 时自动收紧并发与缩略图，减轻随机读
  MB_OLLAMA_HOST  本地 Ollama 地址，默认 http://127.0.0.1:11434（数据不出本机）
  MB_OLLAMA_MODEL 视觉模型名，默认 llava（须 ollama pull 过；也可用 moondream、llava-phi3 等）
  MB_OLLAMA_TIMEOUT  单次请求超时秒数，默认 300
  MB_ANALYZE_FRAME_COUNT  每个视频抽帧送模型，默认 5，范围 2～12
  MB_LOG_LEVEL   日志级别：DEBUG / INFO / WARNING / ERROR（默认 INFO）
  MB_SCAN_ROOT_READONLY  页眉扫描根只读（Docker 可与 MB_SCAN_PRESETS 配合）
  MB_SCAN_PRESETS  媒体库白名单，格式 path|标签;path|标签（设此后页眉为下拉切换；默认不自动扫描，MB_AUTO_SCAN=1 可恢复）
  MB_AUTO_SCAN  启动时是否立即扫描（默认：未设 preset 时为是；设了 MB_SCAN_PRESETS 时为否）
  MB_FFMPEG_HW  播放转码硬件加速：off | auto | vaapi | qsv | nvenc | amf（默认 auto）
  MB_FFMPEG_VAAPI_DEVICE  VAAPI 设备路径（默认 /dev/dri/renderD128 或 renderD*）

完整说明（功能、环境变量、打包、路线图）见项目根目录 README.md。
"""

import os
import sys
import json
import hashlib
import logging
import threading
import subprocess
import time
import mimetypes
import signal
import re
import html as html_mod
import shutil
import uuid
import base64
import secrets
import ipaddress
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse, unquote, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 配置 =====================
APP_VERSION = "2.5.1"
MB_ENABLE_AI = int(os.environ.get("MB_ENABLE_AI", "1"))


def _default_settings_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(base, "Media Browser")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Media Browser")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "media-browser")


SETTINGS_DIR = os.environ.get("MB_CONFIG_DIR") or _default_settings_dir()
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")


def _load_persistent_settings() -> dict:
    try:
        if not os.path.isfile(SETTINGS_PATH):
            return {}
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_persistent_settings(settings: dict) -> None:
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    path = SETTINGS_PATH
    part = path + ".part"
    payload = dict(settings or {})
    with open(part, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(part, path)


_PERSISTENT_SETTINGS = _load_persistent_settings()


def _configured_cache_dir(default: str) -> str:
    env_value = (os.environ.get("MB_CACHE_DIR") or "").strip()
    if env_value:
        return os.path.abspath(os.path.expanduser(env_value))
    saved = _PERSISTENT_SETTINGS.get("cache_dir")
    if isinstance(saved, str) and saved.strip():
        return os.path.abspath(os.path.expanduser(saved.strip()))
    return default


def _default_cache_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return os.path.join(base, "Media Browser", "thumbs")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Media Browser/thumbs")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "media-browser", "thumbs")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            return default
        v = int(str(raw).strip())
        return max(lo, min(hi, v))
    except (ValueError, TypeError):
        return default


def _tool_path(name: str) -> str:
    """打包后与 PATH 中的 ffmpeg/ffprobe：优先使用 PyInstaller 捆绑的可执行文件。"""
    names = [name + ".exe", name] if sys.platform == "win32" else [name]
    directories = [os.path.dirname(os.path.abspath(__file__))]
    if getattr(sys, "frozen", False):
        directories = [getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable)] + directories
    for directory in directories:
        if not directory:
            continue
        for filename in names:
            bundled = os.path.join(directory, filename)
            if os.path.isfile(bundled):
                return bundled
    import shutil as _sh

    w = _sh.which(name)
    return w if w else name


if getattr(sys, "frozen", False):
    _dr = os.path.expanduser("~/Documents/MediaBrowser")
    ROOT_DIR = os.environ.get("MB_ROOT_DIR") or _dr
    CACHE_DIR = _configured_cache_dir(_default_cache_dir())
else:
    if sys.platform == "darwin":
        _default_root = "/Volumes/Untitled/pri"
    else:
        # Windows / Linux 默认使用用户目录下的 MediaBrowser 文件夹，避免指向不存在的卷
        _default_root = os.path.expanduser("~/MediaBrowser")
    ROOT_DIR = os.environ.get("MB_ROOT_DIR", _default_root)
    CACHE_DIR = _configured_cache_dir(_default_cache_dir())

if not (os.environ.get("MB_ROOT_DIR") or "").strip():
    _saved_scan_root = _PERSISTENT_SETTINGS.get("scan_root")
    if isinstance(_saved_scan_root, str) and _saved_scan_root.strip():
        ROOT_DIR = _saved_scan_root.strip()

HOST = os.environ.get("MB_HOST", "127.0.0.1")
PORT = int(os.environ.get("MB_PORT", "8765"))
ACCESS_TOKEN = (os.environ.get("MB_ACCESS_TOKEN") or "").strip()
MAX_BODY_BYTES = _int_env("MB_MAX_BODY_BYTES", 1024 * 1024, 1024, 16 * 1024 * 1024)
PLAY_CACHE_MAX_BYTES = _int_env(
    "MB_PLAY_CACHE_MAX_BYTES",
    20 * 1024 * 1024 * 1024,
    64 * 1024 * 1024,
    1024 * 1024 * 1024 * 1024,
)
THUMB_WIDTH = 400

# 并发默认 2（原 4）：多任务并行会对 NAS/机械盘产生大量随机寻道；SSD 可用 MB_SCAN_WORKERS=4
DISK_PROFILE = os.environ.get("MB_DISK_PROFILE", "").strip().lower()
_DISK_PROFILE_ENV_SET = bool(DISK_PROFILE)
_SCAN_WORKERS_ENV_SET = os.environ.get("MB_SCAN_WORKERS") not in (None, "")
_THUMB_COUNT_ENV_SET = os.environ.get("MB_THUMB_COUNT") not in (None, "")
_MAX_DEFAULT = 2
_THUMB_DEFAULT = 8
if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical"):
    _MAX_DEFAULT = 1

MAX_WORKERS = _int_env("MB_SCAN_WORKERS", _MAX_DEFAULT, 1, 16)
THUMB_COUNT = _int_env("MB_THUMB_COUNT", _THUMB_DEFAULT, 1, 30)
if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical"):
    MAX_WORKERS = min(MAX_WORKERS, 2)


def _ollama_config() -> tuple:
    host = (os.environ.get("MB_OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    model = (os.environ.get("MB_OLLAMA_MODEL") or "llava").strip() or "llava"
    frames = _int_env("MB_ANALYZE_FRAME_COUNT", 5, 2, 12)
    try:
        to = int(os.environ.get("MB_OLLAMA_TIMEOUT", "300"))
        timeout = max(30, min(1800, to))
    except (ValueError, TypeError):
        timeout = 300
    return host, model, frames, timeout


def _path_disk_profile(path: str) -> str:
    """按扫描路径推断磁盘类型；仅在未显式设置 MB_DISK_PROFILE 时使用。"""
    p = os.path.realpath(os.path.abspath(os.path.expanduser(path or ""))).lower()
    # 常见网络挂载提示：smb/nfs/afp/webdav 等
    network_hints = ("/net/", "/network/", "smb://", "afp://", "nfs://", "webdav://")
    if any(h in p for h in network_hints):
        return "nas"
    # macOS 上进一步用 mount 输出判断文件系统类型（远程挂载一般是 smbfs/nfs/afpfs/webdav）
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            for line in (out.stdout or "").splitlines():
                if " on " not in line:
                    continue
                parts = line.split(" on ", 1)[1].split(" (", 1)
                if not parts:
                    continue
                mp = parts[0].strip()
                if not mp:
                    continue
                if p.startswith(mp.lower() + os.sep) or p == mp.lower():
                    fs = (parts[1].lower() if len(parts) > 1 else "")
                    if any(x in fs for x in ("smbfs", "nfs", "afpfs", "webdav")):
                        return "nas"
                    break
    except Exception:
        pass
    return "fast"


def _apply_perf_profile_for_scan_root(scan_root: str) -> str:
    """按扫描根目录自动设置并发/缩略图。显式环境变量优先。"""
    global DISK_PROFILE, MAX_WORKERS, THUMB_COUNT
    profile = DISK_PROFILE if _DISK_PROFILE_ENV_SET else _path_disk_profile(scan_root)
    DISK_PROFILE = profile
    if _SCAN_WORKERS_ENV_SET:
        MAX_WORKERS = _int_env("MB_SCAN_WORKERS", MAX_WORKERS, 1, 16)
    else:
        MAX_WORKERS = 1 if profile in ("slow", "nas", "hdd", "mechanical") else 6
    if _THUMB_COUNT_ENV_SET:
        THUMB_COUNT = _int_env("MB_THUMB_COUNT", THUMB_COUNT, 1, 30)
    else:
        THUMB_COUNT = 8
    if profile in ("slow", "nas", "hdd", "mechanical"):
        MAX_WORKERS = min(MAX_WORKERS, 2)
    return profile

FFMPEG_BIN = _tool_path("ffmpeg")
FFPROBE_BIN = _tool_path("ffprobe")

# 与 PLAY_TRANSCODE_EXTS 对齐：.mts 等若未列入则枚举阶段会完全忽略该格式
VIDEO_EXTS = {
    ".mp4", ".avi", ".mov", ".mkv", ".ts", ".mts", ".qt", ".m4v", ".flv", ".wmv",
    ".webm", ".mpg", ".mpeg", ".3gp", ".m2ts", ".vob",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
# 枚举时跳过系统废纸篓名；其它以「.」开头的文件夹仍会扫描（常见网络盘/相册目录）
_SKIP_SCAN_SUBDIR_NAMES = frozenset({".Trash", ".Trashes"})
# 浏览器常无法直接播放的封装 → 走 /play 经 ffmpeg 转 H.264 + AAC（分片 MP4）
PLAY_TRANSCODE_EXTS = frozenset({".avi", ".ts", ".mts", ".m2ts", ".wmv", ".vob", ".flv"})
# 手机浏览器通常可直接播放（走 /file + Range）；其余格式走 /play 转码
MOBILE_NATIVE_PLAY_EXTS = frozenset({".mp4", ".m4v", ".mov", ".3gp"})
# MP4/MOV are containers: browser playback is reliable for AVC/H.264, while
# HEVC/H.265, MPEG-4 Part 2, ProRes, etc. need the /play transcode path.
MP4_BROWSER_NATIVE_CODECS = frozenset({"h264", "avc1"})
WEBM_BROWSER_NATIVE_CODECS = frozenset({"vp8", "vp9", "av1"})

if getattr(sys, "frozen", False):
    os.makedirs(ROOT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

_scan_root = os.path.realpath(os.path.abspath(os.path.expanduser(ROOT_DIR)))

# 进程启动时刻（用于 /health.uptime_seconds）
_APP_BOOT_MONOTONIC = time.monotonic()


class _JsonLogFormatter(logging.Formatter):
    """单行 JSON，便于生产环境采集（Loki / CloudWatch 等）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _ColorTextFormatter(logging.Formatter):
    """开发用：终端彩色整行前缀。"""

    _RESET = "\x1b[0m"
    _COLORS = {
        logging.DEBUG: "\x1b[36m",
        logging.INFO: "\x1b[32m",
        logging.WARNING: "\x1b[33m",
        logging.ERROR: "\x1b[31m",
        logging.CRITICAL: "\x1b[35m",
    }

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if not getattr(sys.stderr, "isatty", lambda: False)():
            return line
        c = self._COLORS.get(record.levelno, "")
        return f"{c}{line}{self._RESET}" if c else line


def setup_logging() -> logging.Logger:
    """
    MB_LOG_LEVEL: DEBUG / INFO / WARNING / ERROR（默认 INFO）
    MB_LOG_FORMAT: text | json（默认 text；json 为单行结构化）
    """
    lg = logging.getLogger("media_browser")
    if lg.handlers:
        return lg
    level_name = (os.environ.get("MB_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = (os.environ.get("MB_LOG_FORMAT") or "text").strip().lower()
    h = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        h.setFormatter(_JsonLogFormatter())
    else:
        use_color = sys.stderr.isatty()
        if use_color:
            h.setFormatter(
                _ColorTextFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
            )
        else:
            h.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
            )
    lg.setLevel(level)
    lg.addHandler(h)
    lg.propagate = False
    return lg


logger = setup_logging()


def scan_root_readonly() -> bool:
    """Docker 等场景：页眉扫描根只读（改路径请改 compose 挂载）。"""
    return os.environ.get("MB_SCAN_ROOT_READONLY", "").strip().lower() in (
        "1",
        "yes",
        "true",
        "on",
    )


def get_scan_root() -> str:
    return _scan_root


def ffprobe_has_audio(path: str) -> bool:
    try:
        r = subprocess.run(
            [
                FFPROBE_BIN, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=index", "-of", "csv=p=0", path,
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def video_needs_transcoded_play(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PLAY_TRANSCODE_EXTS


def normalize_video_codec(codec) -> str:
    c = re.sub(r"[^a-z0-9]+", "", str(codec or "").strip().lower())
    if c in ("avc", "avc1", "h264", "x264"):
        return "h264"
    if c in ("hevc", "h265", "hvc1", "hev1", "x265"):
        return "hevc"
    return c


def video_codec_needs_transcoded_play(path: str, codec=None) -> bool:
    c = normalize_video_codec(codec)
    if not c:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in MOBILE_NATIVE_PLAY_EXTS:
        return c not in MP4_BROWSER_NATIVE_CODECS
    if ext == ".webm":
        return c not in WEBM_BROWSER_NATIVE_CODECS
    return False


def video_play_needs_transcode(path: str, mobile: bool = False, codec=None) -> bool:
    return video_should_use_play_endpoint(path, mobile=mobile, codec=codec)


def video_should_use_play_endpoint(path: str, mobile: bool = False, codec=None) -> bool:
    """是否应走 GET /play（实时转 H.264）。手机端 mp4/mov 等优先 /file 直出。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in VIDEO_EXTS:
        return False
    if ext in PLAY_TRANSCODE_EXTS:
        return True
    if video_codec_needs_transcoded_play(path, codec):
        return True
    if ext in MOBILE_NATIVE_PLAY_EXTS:
        return False
    if mobile and ext not in MOBILE_NATIVE_PLAY_EXTS:
        return True
    return False


def is_path_under_root(path: str) -> bool:
    """解析后的路径必须位于当前扫描根目录之下（含根目录本身），用于限制 /file、/open、删除。"""
    try:
        root = os.path.realpath(get_scan_root())
        target = os.path.realpath(path)
    except OSError:
        return False
    if target == root:
        return True
    return target.startswith(root + os.sep)


def _host_requires_access_token(host: str | None = None) -> bool:
    value = (HOST if host is None else host).strip().lower()
    return value not in ("127.0.0.1", "localhost", "::1")


def _access_token_required() -> bool:
    return bool(ACCESS_TOKEN) or _host_requires_access_token()


def _prune_walk_dirs(dirs: list[str], current_root: str, scan_root: str, seen: set[str]) -> None:
    """Keep walk traversal inside scan_root and avoid symlink directory loops."""
    scan_real = os.path.realpath(scan_root)
    kept: list[str] = []
    for name in dirs:
        if name in _SKIP_SCAN_SUBDIR_NAMES:
            continue
        real = os.path.realpath(os.path.join(current_root, name))
        if real != scan_real and not real.startswith(scan_real + os.sep):
            continue
        key = os.path.normcase(real)
        if key in seen:
            continue
        seen.add(key)
        kept.append(name)
    dirs[:] = kept


# 通用占位图
PLACEHOLDER = os.path.join(CACHE_DIR, "_placeholder.jpg")


def _ensure_cache_dir_ready() -> None:
    global PLACEHOLDER
    os.makedirs(CACHE_DIR, exist_ok=True)
    PLACEHOLDER = os.path.join(CACHE_DIR, "_placeholder.jpg")
    if not os.path.exists(PLACEHOLDER):
        subprocess.run([
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=#1a1a1a:s=400x300",
            "-frames:v", "1", "-q:v", "3", PLACEHOLDER
        ], capture_output=True)


_ensure_cache_dir_ready()

# ===================== 工具函数 =====================
def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def _thumb_cache_dir(file_hash: str) -> str:
    old_dir = os.path.join(CACHE_DIR, file_hash)
    if os.path.isdir(old_dir):
        return old_dir
    return os.path.join(CACHE_DIR, file_hash[:2], file_hash)


def _play_cache_key(source: str) -> str:
    rp = os.path.realpath(source)
    st = os.stat(rp)
    raw = f"{rp}\n{st.st_mtime_ns}\n{st.st_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def play_cache_root() -> str:
    return os.path.join(CACHE_DIR, "play_mp4")


def play_cache_path(source: str) -> str:
    key = _play_cache_key(source)
    return os.path.join(play_cache_root(), key[:2], key + ".mp4")


def is_play_cache_file(path: str) -> bool:
    try:
        rp = os.path.realpath(path)
        base = os.path.realpath(play_cache_root())
    except OSError:
        return False
    if not rp.startswith(base + os.sep):
        return False
    return os.path.isfile(rp)


def is_servable_file_path(path: str) -> bool:
    return is_path_under_root(path) or is_play_cache_file(path)


def prune_play_cache(max_bytes: int | None = None) -> int:
    """Remove oldest play-ready cache files until the configured size limit is met."""
    limit = PLAY_CACHE_MAX_BYTES if max_bytes is None else max(0, int(max_bytes))
    root = play_cache_root()
    files: list[tuple[float, int, str]] = []
    total = 0
    if not os.path.isdir(root):
        return 0
    for current, _dirs, names in os.walk(root):
        for name in names:
            if not name.endswith(".mp4"):
                continue
            path = os.path.join(current, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            total += st.st_size
            files.append((st.st_mtime, st.st_size, path))
    removed = 0
    for _mtime, size, path in sorted(files):
        if total <= limit:
            break
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


_play_transcode_guard = threading.Lock()
_play_transcode_jobs: dict[str, dict] = {}
_play_transcode_locks: dict[str, threading.Lock] = {}


def _play_job_lock(key: str) -> threading.Lock:
    with _play_transcode_guard:
        if key not in _play_transcode_locks:
            _play_transcode_locks[key] = threading.Lock()
        return _play_transcode_locks[key]


_ffmpeg_hw_lock = threading.Lock()
_ffmpeg_hw_cached: dict | None = None
_ffmpeg_encoder_usable_cache: dict[str, bool] = {}


def _ffmpeg_hw_configured() -> str:
    raw = (os.environ.get("MB_FFMPEG_HW") or "auto").strip().lower()
    if raw in ("off", "0", "false", "no"):
        return "off"
    if raw in ("1", "true", "yes", "on"):
        return "auto"
    if raw in ("auto", "vaapi", "qsv", "nvenc", "amf"):
        return raw
    return "auto"


def _ffmpeg_vaapi_device_path() -> str | None:
    env = (os.environ.get("MB_FFMPEG_VAAPI_DEVICE") or "").strip()
    if env and os.path.exists(env):
        return env
    render = "/dev/dri/renderD128"
    if os.path.exists(render):
        return render
    dri = "/dev/dri"
    if os.path.isdir(dri):
        try:
            for name in sorted(os.listdir(dri)):
                if name.startswith("renderD"):
                    return os.path.join(dri, name)
        except OSError:
            pass
    return None


def _ffmpeg_has_encoder(encoder: str) -> bool:
    try:
        r = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return encoder in (r.stdout or "")
    except Exception:
        return False


def _ffmpeg_null_output() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def _ffmpeg_encoder_test_args(mode: str) -> list[str]:
    encoder = {
        "qsv": "h264_qsv",
        "nvenc": "h264_nvenc",
        "amf": "h264_amf",
    }.get(mode)
    if not encoder:
        return []
    args = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=1",
        "-frames:v", "1",
        "-c:v", encoder,
    ]
    if mode == "nvenc":
        args += ["-preset", "fast"]
    elif mode == "amf":
        args += ["-quality", "speed"]
    elif mode == "qsv":
        args += ["-preset", "veryfast"]
    args += ["-f", "null", _ffmpeg_null_output()]
    return args


def _ffmpeg_encoder_runtime_usable(mode: str) -> bool:
    """Return whether the hardware encoder can actually run on this machine."""
    if mode in _ffmpeg_encoder_usable_cache:
        return _ffmpeg_encoder_usable_cache[mode]
    encoder = {
        "qsv": "h264_qsv",
        "nvenc": "h264_nvenc",
        "amf": "h264_amf",
    }.get(mode)
    ok = False
    if encoder and _ffmpeg_has_encoder(encoder):
        try:
            r = subprocess.run(
                _ffmpeg_encoder_test_args(mode),
                capture_output=True,
                text=True,
                timeout=15,
            )
            ok = r.returncode == 0
        except Exception:
            ok = False
    _ffmpeg_encoder_usable_cache[mode] = ok
    return ok


def _invalidate_ffmpeg_hw_cache() -> None:
    global _ffmpeg_hw_cached, _ffmpeg_encoder_usable_cache
    with _ffmpeg_hw_lock:
        _ffmpeg_hw_cached = None
        _ffmpeg_encoder_usable_cache = {}


def resolve_ffmpeg_hw() -> dict:
    """解析 MB_FFMPEG_HW：返回 configured / active / device / available / error。"""
    global _ffmpeg_hw_cached
    with _ffmpeg_hw_lock:
        if _ffmpeg_hw_cached is not None:
            return dict(_ffmpeg_hw_cached)
        configured = _ffmpeg_hw_configured()
        device = _ffmpeg_vaapi_device_path()
        active = "off"
        error: str | None = None
        details: list[str] = []
        if configured == "off":
            pass
        elif configured == "vaapi":
            if not device:
                error = "未找到 /dev/dri/renderD*（Docker 需挂载 devices 与 group_add render）"
            elif not os.access(device, os.R_OK | os.W_OK):
                error = f"无法访问 VAAPI 设备 {device}"
            elif not _ffmpeg_has_encoder("h264_vaapi"):
                error = "ffmpeg 无 h264_vaapi 编码器（镜像需 intel-media-va-driver）"
            else:
                active = "vaapi"
        elif configured == "qsv":
            if not _ffmpeg_has_encoder("h264_qsv"):
                error = "ffmpeg 无 h264_qsv 编码器"
            else:
                active = "qsv"
            if active == "qsv" and not _ffmpeg_encoder_runtime_usable("qsv"):
                active = "off"
                error = "h264_qsv 无法在当前机器运行（可能没有 Intel 核显/驱动）"
        elif configured == "nvenc":
            if not _ffmpeg_has_encoder("h264_nvenc"):
                error = "ffmpeg 无 h264_nvenc 编码器"
            elif not _ffmpeg_encoder_runtime_usable("nvenc"):
                error = "h264_nvenc 无法在当前机器运行（可能没有 NVIDIA 显卡/驱动）"
            else:
                active = "nvenc"
        elif configured == "amf":
            if not _ffmpeg_has_encoder("h264_amf"):
                error = "ffmpeg 无 h264_amf 编码器"
            elif not _ffmpeg_encoder_runtime_usable("amf"):
                error = "h264_amf 无法在当前机器运行（可能没有 AMD 显卡/驱动）"
            else:
                active = "amf"
        elif configured == "auto":
            candidates = ["qsv", "nvenc", "amf"] if os.name == "nt" else ["vaapi", "qsv", "nvenc", "amf"]
            for mode in candidates:
                why = None
                if mode == "vaapi":
                    if not device:
                        why = "未找到 /dev/dri/renderD*"
                    elif not os.access(device, os.R_OK | os.W_OK):
                        why = f"无法访问 VAAPI 设备 {device}"
                    elif not _ffmpeg_has_encoder("h264_vaapi"):
                        why = "ffmpeg 无 h264_vaapi 编码器"
                    else:
                        active = "vaapi"
                else:
                    encoder = {"qsv": "h264_qsv", "nvenc": "h264_nvenc", "amf": "h264_amf"}[mode]
                    if not _ffmpeg_has_encoder(encoder):
                        why = f"ffmpeg 无 {encoder} 编码器"
                    elif not _ffmpeg_encoder_runtime_usable(mode):
                        why = f"{encoder} 无法在当前机器运行"
                    else:
                        active = mode
                if active != "off":
                    break
                if why:
                    details.append(f"{mode}: {why}")
        if configured == "auto" and active == "off" and error:
            error = None
        _ffmpeg_hw_cached = {
            "configured": configured,
            "active": active,
            "device": device if active == "vaapi" else None,
            "available": active != "off",
            "error": error,
            "details": details,
        }
        return dict(_ffmpeg_hw_cached)


def _ffmpeg_x264_preset() -> str:
    return "ultrafast" if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical") else "veryfast"


def _ffmpeg_sw_video_encode_args(preset: str) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", preset, "-crf", "23",
        "-profile:v", "baseline", "-level", "3.1",
        "-pix_fmt", "yuv420p",
    ]


def _ffmpeg_thumb_hwaccel_args() -> list[str]:
    hw = resolve_ffmpeg_hw()
    active = hw.get("active") or "off"
    if active in ("vaapi", "qsv", "nvenc", "amf"):
        return ["-hwaccel", "auto"]
    return []


def _ffmpeg_build_transcode_cmd(
    source: str,
    dest_or_pipe: str,
    *,
    has_audio: bool,
    hw_mode: str,
    vaapi_device: str | None,
    for_pipe: bool,
) -> list[str]:
    preset = _ffmpeg_x264_preset()
    cmd = [FFMPEG_BIN]
    if not for_pipe:
        cmd.append("-y")
    cmd += ["-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "2"]
    if hw_mode == "vaapi" and vaapi_device:
        cmd += [
            "-vaapi_device", vaapi_device,
            "-fflags", "+genpts", "-err_detect", "ignore_err",
            "-i", source,
            "-map", "0:v:0",
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi",
            "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
            "-profile:v", "66", "-level", "31",
        ]
    elif hw_mode == "qsv":
        cmd += [
            "-init_hw_device", "qsv=hw",
            "-filter_hw_device", "hw",
            "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
            "-fflags", "+genpts", "-err_detect", "ignore_err",
            "-i", source,
            "-map", "0:v:0",
            "-c:v", "h264_qsv", "-preset", "veryfast",
            "-profile:v", "baseline", "-level", "3.1",
        ]
    elif hw_mode == "nvenc":
        cmd += [
            "-hwaccel", "auto",
            "-fflags", "+genpts", "-err_detect", "ignore_err",
            "-i", source,
            "-map", "0:v:0",
            "-c:v", "h264_nvenc", "-preset", "fast",
            "-profile:v", "main", "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
        ]
    elif hw_mode == "amf":
        cmd += [
            "-hwaccel", "auto",
            "-fflags", "+genpts", "-err_detect", "ignore_err",
            "-i", source,
            "-map", "0:v:0",
            "-c:v", "h264_amf", "-quality", "speed",
            "-profile:v", "main", "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
        ]
    else:
        cmd += [
            "-fflags", "+genpts", "-err_detect", "ignore_err",
            "-i", source,
            "-map", "0:v:0",
            *_ffmpeg_sw_video_encode_args(preset),
        ]
    if has_audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    else:
        cmd += ["-an"]
    if for_pipe:
        cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", dest_or_pipe]
    else:
        cmd += ["-movflags", "+faststart", "-f", "mp4", dest_or_pipe]
    return cmd


def _ffmpeg_run_transcode(cmd: list[str], source: str, *, timeout: int = 7200) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _register_ffmpeg_proc(source, proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise
    finally:
        _unregister_ffmpeg_proc(source, proc)
    if proc.returncode != 0:
        err = (stderr or stdout or "ffmpeg failed").strip()[:500]
        raise RuntimeError(err or "ffmpeg failed")


def _try_ts_remux(source: str, dest: str) -> bool:
    """Copy browser-compatible TS video packets; originals are never rewritten."""
    if os.path.splitext(source)[1].lower() not in {".ts", ".mts", ".m2ts"}:
        return False
    try:
        probe = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt", "-of", "json", source],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        streams = json.loads(probe.stdout).get("streams", [])
        if not streams or streams[0].get("codec_name") != "h264" or streams[0].get("pix_fmt") not in {"yuv420p", "yuvj420p"}:
            return False
        cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-fflags", "+genpts", "-i", source, "-map", "0:v:0", "-map", "0:a:0?",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
               "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", "-f", "mp4", dest]
        _ffmpeg_run_transcode(cmd, source)
        if os.path.isfile(dest) and os.path.getsize(dest) > 512:
            logger.info("TS playback cache: video stream copied without re-encoding")
            return True
    except Exception as exc:
        logger.warning("TS remux failed; using compatible encoding: %s", str(exc)[:200])
    return False


def _ffmpeg_transcode_to_mp4(source: str, dest: str) -> None:
    if _try_ts_remux(source, dest):
        return
    has_audio = ffprobe_has_audio(source)
    hw = resolve_ffmpeg_hw()
    active = hw.get("active") or "off"
    device = hw.get("device")
    if active in ("vaapi", "qsv", "nvenc", "amf"):
        cmd = _ffmpeg_build_transcode_cmd(
            source,
            dest,
            has_audio=has_audio,
            hw_mode=active,
            vaapi_device=device,
            for_pipe=False,
        )
        try:
            _ffmpeg_run_transcode(cmd, source)
            if os.path.isfile(dest) and os.path.getsize(dest) >= 512:
                logger.info("play 转码使用 %s（%s）", active, device or active)
                return
            raise RuntimeError("转码输出为空或过小")
        except Exception as e:
            logger.warning("play 转码 %s 失败，回退 CPU: %s", active, str(e)[:200])
    cmd = _ffmpeg_build_transcode_cmd(
        source,
        dest,
        has_audio=has_audio,
        hw_mode="off",
        vaapi_device=None,
        for_pipe=False,
    )
    _ffmpeg_run_transcode(cmd, source)
    if not os.path.isfile(dest) or os.path.getsize(dest) < 512:
        raise RuntimeError("转码输出为空或过小")


def _play_transcode_worker(source: str, key: str) -> None:
    out = play_cache_path(source)
    part = out + ".part"
    try:
        prune_play_cache()
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with _play_job_lock(key):
            if os.path.isfile(out) and os.path.getsize(out) > 512:
                st = {"status": "ready", "error": None, "finished": time.monotonic()}
            else:
                if os.path.isfile(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                _ffmpeg_transcode_to_mp4(source, part)
                os.replace(part, out)
                prune_play_cache()
                if not os.path.isfile(out):
                    raise RuntimeError("转码文件超过 MB_PLAY_CACHE_MAX_BYTES 缓存上限")
                st = {"status": "ready", "error": None, "finished": time.monotonic()}
        with _play_transcode_guard:
            _play_transcode_jobs[key] = st
    except Exception as e:
        logger.warning("play 转码失败 %s: %s", source, e)
        try:
            if os.path.isfile(part):
                os.remove(part)
        except OSError:
            pass
        with _play_transcode_guard:
            _play_transcode_jobs[key] = {
                "status": "error",
                "error": str(e)[:500],
                "finished": time.monotonic(),
            }


def play_ready_payload(source: str, force_transcode: bool = False) -> dict:
    """GET /api/play-ready — 异步转码到 play_mp4 缓存，完成后用 /file Range 播放。"""
    try:
        rp = os.path.realpath(source)
    except OSError as e:
        return {"ok": False, "ready": False, "status": "error", "error": str(e)}
    if not os.path.isfile(rp) or not is_path_under_root(rp):
        return {"ok": False, "ready": False, "status": "error", "error": "文件不存在或不在扫描根下"}
    ext = os.path.splitext(rp)[1].lower()
    if ext not in VIDEO_EXTS:
        return {"ok": False, "ready": False, "status": "error", "error": "不是支持的视频格式"}
    if not force_transcode and not video_should_use_play_endpoint(rp, mobile=True, codec=get_video_info(rp).get("codec")):
        from urllib.parse import quote

        return {
            "ok": True,
            "ready": True,
            "status": "ready",
            "url": "/file?path=" + quote(rp, safe=""),
        }
    cached = play_cache_path(rp)
    if os.path.isfile(cached) and os.path.getsize(cached) > 512:
        from urllib.parse import quote

        return {
            "ok": True,
            "ready": True,
            "status": "ready",
            "url": "/file?path=" + quote(cached, safe=""),
        }
    fa, fe = _tool_version_ok(FFMPEG_BIN)
    if not fa:
        return {
            "ok": False,
            "ready": False,
            "status": "error",
            "error": f"ffmpeg 不可用: {fe or 'missing'}",
        }
    key = _play_cache_key(rp)
    with _play_transcode_guard:
        job = _play_transcode_jobs.get(key)
        if job and job.get("status") == "working":
            elapsed = int(time.monotonic() - float(job.get("started", time.monotonic())))
            return {"ok": True, "ready": False, "status": "working", "elapsed": elapsed}
        if job and job.get("status") == "error":
            return {
                "ok": False,
                "ready": False,
                "status": "error",
                "error": job.get("error") or "转码失败",
            }
        _play_transcode_jobs[key] = {
            "status": "working",
            "error": None,
            "started": time.monotonic(),
        }
        threading.Thread(
            target=_play_transcode_worker,
            args=(rp, key),
            daemon=True,
        ).start()
    return {"ok": True, "ready": False, "status": "working", "elapsed": 0}


_review_state_lock = threading.Lock()


def _review_state_store_path(scan_root: str | None = None) -> str:
    root = os.path.realpath(scan_root or get_scan_root())
    key = sha256_str(root)
    review_dir = os.path.join(CACHE_DIR, "review")
    os.makedirs(review_dir, exist_ok=True)
    return os.path.join(review_dir, f"{key}.json")



_library_index_cache = {}
_library_index_lock = threading.Lock()

def _library_index_store_path(scan_root: str | None = None) -> str:
    root = os.path.realpath(scan_root or get_scan_root())
    key = sha256_str(root)
    library_dir = os.path.join(CACHE_DIR, "library")
    os.makedirs(library_dir, exist_ok=True)
    return os.path.join(library_dir, f"library_index_{key}.json")

def load_library_index(scan_root: str | None = None) -> dict:
    global _library_index_cache
    path = _library_index_store_path(scan_root)
    with _library_index_lock:
        if not os.path.isfile(path):
            _library_index_cache = {}
            return _library_index_cache
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                _library_index_cache = data if isinstance(data, dict) else {}
        except Exception:
            _library_index_cache = {}
        return _library_index_cache

def save_library_index(scan_root: str | None = None) -> None:
    global _library_index_cache
    path = _library_index_store_path(scan_root)
    with _library_index_lock:
        part = path + ".part"
        try:
            with open(part, "w", encoding="utf-8") as f:
                json.dump(_library_index_cache, f, ensure_ascii=False)
            os.replace(part, path)
        except Exception as e:
            logger.warning("Failed to save library index: %s", e)


def empty_review_state(scan_root: str | None = None) -> dict:
    root = os.path.realpath(scan_root or get_scan_root())
    return {
        "version": 2,
        "scan_root": root,
        "updated_at": None,
        "global": {
            "last_work_id": None,
            "last_item_path": None,
            "last_opened_at": None,
        },
        "works": {},
        "videos": {},
    }


def load_review_state(scan_root: str | None = None) -> dict:
    path = _review_state_store_path(scan_root)
    with _review_state_lock:
        if not os.path.isfile(path):
            return empty_review_state(scan_root)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return empty_review_state(scan_root)
        if not isinstance(data, dict):
            return empty_review_state(scan_root)
        # v2 keeps the JSON ledger portable while extending each work with
        # rating/features/categories and optional AI suggestions.  Old v1
        # ledgers are upgraded lazily and written as v2 on the next change.
        data["version"] = 2
        data.setdefault("global", {})
        data.setdefault("works", {})
        data.setdefault("videos", {})
        if not isinstance(data["global"], dict):
            data["global"] = {}
        if not isinstance(data["works"], dict):
            data["works"] = {}
        if not isinstance(data["videos"], dict):
            data["videos"] = {}
        data["global"].setdefault("last_work_id", None)
        data["global"].setdefault("last_item_path", None)
        data["global"].setdefault("last_opened_at", None)
        return data


def save_review_state(state: dict, scan_root: str | None = None) -> None:
    path = _review_state_store_path(scan_root)
    root = os.path.realpath(scan_root or get_scan_root())
    payload = dict(state)
    payload["version"] = 2
    payload["scan_root"] = root
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload.setdefault("global", {})
    payload.setdefault("works", {})
    payload.setdefault("videos", {})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    with _review_state_lock:
        with open(part, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(part, path)


def review_state_get_payload() -> dict:
    state = load_review_state()
    return {
        "ok": True,
        "state": state,
        "store_path": _review_state_store_path(),
    }


def patch_review_state_work(work_id: str, patch: dict) -> dict:
    if not work_id or not isinstance(work_id, str):
        return {"ok": False, "error": "invalid work_id"}
    if not isinstance(patch, dict):
        return {"ok": False, "error": "invalid patch"}
    state = load_review_state()
    works = state.setdefault("works", {})
    entry = dict(works.get(work_id) or {})
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if "tag" in patch:
        tag = patch.get("tag")
        if tag not in ("kept", "pending", "deleted"):
            return {"ok": False, "error": "tag must be kept, pending or deleted"}
        entry["tag"] = tag
        entry["tag_updated_at"] = now

    if "last_item_path" in patch:
        lip = patch.get("last_item_path")
        entry["last_item_path"] = lip if isinstance(lip, str) and lip.strip() else None
        entry["last_opened_at"] = now
    if "last_item_idx" in patch:
        try:
            entry["last_item_idx"] = int(patch["last_item_idx"])
        except (TypeError, ValueError):
            pass

    works[work_id] = entry
    gl = state.setdefault("global", {})
    if patch.get("update_global", True):
        gl["last_work_id"] = work_id
        if "last_item_path" in patch:
            gl["last_item_path"] = entry.get("last_item_path")
        gl["last_opened_at"] = now

    save_review_state(state)
    return {"ok": True, "work_id": work_id, "work": entry, "global": gl}


@lru_cache(maxsize=10000)
def _cached_video_asset_id(path: str, mtime: float, size: int) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
        import hashlib
        h = hashlib.sha256()
        h.update(f"{size}:".encode("utf-8"))
        h.update(head)
        return h.hexdigest()
    except Exception:
        return sha256_str(os.path.normcase(os.path.realpath(path)))

def video_asset_id(path: str) -> str:
    try:
        st = os.stat(path)
        return _cached_video_asset_id(path, st.st_mtime, st.st_size)
    except OSError:
        return sha256_str(os.path.normcase(os.path.realpath(path)))


def patch_review_state_video(video_id: str, patch: dict) -> dict:
    if not video_id or not re.fullmatch(r"[0-9a-fA-F]{16,64}", video_id):
        return {"ok": False, "error": "invalid video_id"}
    if not isinstance(patch, dict):
        return {"ok": False, "error": "invalid patch"}
    state = load_review_state()
    videos = state.setdefault("videos", {})
    entry = dict(videos.get(video_id) or {})
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if "path" in patch:
        path = patch.get("path")
        if not isinstance(path, str) or video_asset_id(path) != video_id:
            return {"ok": False, "error": "path does not match video_id"}
        entry["path"] = os.path.realpath(path)
    if "tag" in patch:
        tag = patch.get("tag")
        if tag not in ("kept", "pending", "deleted"):
            return {"ok": False, "error": "tag must be kept, pending or deleted"}
        entry["tag"] = tag
        entry["tag_updated_at"] = now
    if "rating" in patch:
        raw_rating = patch.get("rating")
        if raw_rating is None:
            entry.pop("rating", None)
            entry["tag"] = "pending"
        else:
            try:
                rating = int(raw_rating)
            except (TypeError, ValueError):
                return {"ok": False, "error": "rating must be an integer from 0 to 5 or null"}
            if not 0 <= rating <= 5:
                return {"ok": False, "error": "rating must be an integer from 0 to 5 or null"}
            entry["rating"] = rating
            entry["tag"] = "deleted" if rating == 0 else "kept"
            existing_ai = entry.get("ai_analysis") if isinstance(entry.get("ai_analysis"), dict) else {}
            if existing_ai.get("status") != "done":
                entry["ai_analysis"] = dict(existing_ai, status="queued", queued_at=now)
        entry["rating_updated_at"] = now
        entry["tag_updated_at"] = now
    # These fields are machine-owned: the UI never asks the user to author them.
    if "ai_analysis" in patch:
        value = patch.get("ai_analysis")
        if not isinstance(value, dict):
            return {"ok": False, "error": "ai_analysis must be an object"}
        entry["ai_analysis"] = value
        entry["ai_analysis_updated_at"] = now
    if "features" in patch:
        value = patch.get("features")
        if not isinstance(value, dict):
            return {"ok": False, "error": "features must be an object"}
        entry["features"] = value
        entry["features_updated_at"] = now
    if "categories" in patch:
        value = patch.get("categories")
        if not isinstance(value, list):
            return {"ok": False, "error": "categories must be an array"}
        entry["categories"] = list(dict.fromkeys(str(x).strip()[:80] for x in value if str(x).strip()))[:30]
        entry["categories_updated_at"] = now
    videos[video_id] = entry
    save_review_state(state)
    return {"ok": True, "video_id": video_id, "video": entry}


def save_automatic_video_analysis(path: str, insight: dict, provider: str, model: str) -> dict:
    """Persist model output as video-owned features/categories."""
    vid = video_asset_id(path)
    tags = insight.get("tags") or []
    if isinstance(tags, str):
        tags = [x.strip() for x in re.split(r"[,，;；|/]", tags) if x.strip()]
    dimensions = [
        ("人数", insight.get("people_count")),
        ("场景", insight.get("scene_type")),
        ("制作", insight.get("production_type")),
        ("镜头", insight.get("camera_style")),
        ("叙事", insight.get("story_level")),
        ("语言", insight.get("audio_language")),
        ("地区", insight.get("content_region")),
    ]
    unavailable = {"不确定", "无音轨", "无可识别语音"}
    categories = [f"{name}:{value}" for name, value in dimensions if value and value not in unavailable]
    categories.extend(str(x).strip() for x in (insight.get("distinctive_features") or []) if str(x).strip())
    categories.extend(f"演员:{name}" for name in (insight.get("performers") or []) if str(name).strip())
    if insight.get("studio"):
        categories.append(f"制作方:{insight['studio']}")
    if insight.get("title_code"):
        categories.append(f"编号:{insight['title_code']}")
    if not categories:
        categories.extend(str(x).strip() for x in tags if str(x).strip())
    categories = list(dict.fromkeys(categories))[:30]
    features = {
        "时间": insight.get("time_guess") or "",
        "地点": insight.get("place_guess") or "",
        "事件": insight.get("event_guess") or "",
        "内容描述": insight.get("phrase") or "",
        "人物数量": insight.get("people_count") or "",
        "场景类型": insight.get("scene_type") or "",
        "制作类型": insight.get("production_type") or "",
        "镜头风格": insight.get("camera_style") or "",
        "叙事程度": insight.get("story_level") or "",
        "置信度": insight.get("confidence") if insight.get("confidence") is not None else "",
        "演员": "、".join(insight.get("performers") or []),
        "候选演员": "、".join(insight.get("performer_candidates") or []),
        "身份依据": json.dumps(insight.get("performer_evidence") or {}, ensure_ascii=False),
        "制作方": insight.get("studio") or "",
        "作品编号": insight.get("title_code") or "",
        "身份置信度": insight.get("identity_confidence") if insight.get("identity_confidence") is not None else "",
        "音频语言": insight.get("audio_language") or "",
        "音频语言代码": insight.get("audio_language_code") or "",
        "语言置信度": insight.get("audio_language_confidence") if insight.get("audio_language_confidence") is not None else "",
        "地区来源": insight.get("content_region") or "",
        "地区置信度": insight.get("region_confidence") if insight.get("region_confidence") is not None else "",
        "地区依据": "；".join(str(x) for x in (insight.get("region_evidence") or []) if str(x).strip()),
    }
    features = {k: v for k, v in features.items() if v}
    return patch_review_state_video(vid, {
        "path": path,
        "categories": categories,
        "features": features,
        "ai_analysis": {"provider": provider, "model": model, "status": "done", "schema_version": 4, "raw": insight},
    })


def preference_summary_payload() -> dict:
    """Derive transparent classification evidence from the user's own labels."""
    state = load_review_state()
    videos = state.get("videos") or {}
    tag_counts = {"kept": 0, "pending": 0, "deleted": 0}
    rating_counts = {str(i): 0 for i in range(1, 6)}
    categories: dict[str, dict] = {}
    features: dict[str, dict[str, int]] = {}
    rated = 0
    for entry in videos.values():
        if not isinstance(entry, dict):
            continue
        tag = entry.get("tag", "pending")
        if tag in tag_counts:
            tag_counts[tag] += 1
        rating = entry.get("rating", 0)
        if isinstance(rating, int) and 1 <= rating <= 5:
            rating_counts[str(rating)] += 1
            rated += 1
        for label in entry.get("categories") or []:
            if not isinstance(label, str) or not label.strip():
                continue
            row = categories.setdefault(label.strip(), {"total": 0, "kept": 0, "deleted": 0})
            row["total"] += 1
            if tag in ("kept", "deleted"):
                row[tag] += 1
        for key, value in (entry.get("features") or {}).items():
            if not isinstance(key, str) or isinstance(value, (dict, list)):
                continue
            val = str(value).strip()
            if val:
                bucket = features.setdefault(key.strip(), {})
                bucket[val] = bucket.get(val, 0) + 1
    category_rows = [dict(name=name, **counts) for name, counts in categories.items()]
    category_rows.sort(key=lambda row: (-row["total"], row["name"].lower()))
    feature_rows = []
    for name, values in features.items():
        top_values = sorted(values.items(), key=lambda pair: (-pair[1], pair[0].lower()))[:10]
        feature_rows.append({"name": name, "values": [{"value": v, "count": n} for v, n in top_values]})
    feature_rows.sort(key=lambda row: row["name"].lower())
    return {
        "ok": True,
        "ledger_version": 2,
        "reviewed": tag_counts["kept"] + tag_counts["deleted"],
        "rated": rated,
        "tag_counts": tag_counts,
        "rating_counts": rating_counts,
        "categories": category_rows,
        "features": feature_rows,
    }


ANALYSIS_AUTO_ACCEPT_CONFIDENCE = 0.80
ANALYSIS_REVIEW_CONFIDENCE = 0.55
ANALYSIS_REQUIRED_DIMENSIONS = (
    "people_count",
    "scene_type",
    "production_type",
    "camera_style",
    "story_level",
)


def _analysis_confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def analysis_review_decision(entry: dict) -> dict:
    """Return a small, explainable review bucket for one video ledger entry."""
    entry = entry if isinstance(entry, dict) else {}
    ai = entry.get("ai_analysis") if isinstance(entry.get("ai_analysis"), dict) else {}
    raw = ai.get("raw") if isinstance(ai.get("raw"), dict) else {}
    manual = entry.get("analysis_review") if isinstance(entry.get("analysis_review"), dict) else {}
    manual_status = manual.get("status")
    if manual_status in ("accepted", "excluded"):
        return {
            "status": manual_status,
            "reason": "已人工接受" if manual_status == "accepted" else "已从自动分类中排除",
            "confidence": _analysis_confidence(raw.get("confidence")),
            "completeness": 0,
            "identity_pending": False,
        }

    ai_status = str(ai.get("status") or "missing")
    confidence = _analysis_confidence(raw.get("confidence"))
    present = sum(
        1 for key in ANALYSIS_REQUIRED_DIMENSIONS
        if str(raw.get(key) or "").strip() not in ("", "不确定", "未知")
    )
    candidates = [str(x).strip() for x in (raw.get("performer_candidates") or []) if str(x).strip()]
    confirmed = [str(x).strip() for x in (raw.get("performers") or []) if str(x).strip()]
    identity_pending = bool(candidates and not confirmed)
    if ai_status in ("queued", "running"):
        status, reason = "processing", "正在排队或分析"
    elif ai_status == "error":
        status, reason = "attention", "分析失败，需要重试"
    elif ai_status != "done":
        status, reason = "attention", "尚未得到有效分析结果"
    elif confidence >= ANALYSIS_AUTO_ACCEPT_CONFIDENCE and present >= 4:
        status, reason = "auto_accepted", "高置信度且主要分类完整，已自动接受"
    elif confidence >= ANALYSIS_REVIEW_CONFIDENCE:
        status, reason = "review", "置信度一般或分类不够完整"
    else:
        status, reason = "attention", "置信度较低，需要重点复核"
    return {
        "status": status,
        "reason": reason,
        "confidence": confidence,
        "completeness": present,
        "identity_pending": identity_pending,
    }


def analysis_review_payload() -> dict:
    """Build the exception-review dashboard from current per-video state."""
    state = load_review_state()
    ledger = state.get("videos") or {}
    paths = []
    progress = scanner.get_progress(0)
    for work in progress.get("works") or []:
        for item in work.get("items") or []:
            path = item.get("path")
            if item.get("type") == "video" and isinstance(path, str):
                paths.append(path)
    if not paths:
        paths = [entry.get("path") for entry in ledger.values() if isinstance(entry, dict) and entry.get("path")]

    counts = {key: 0 for key in ("auto_accepted", "accepted", "review", "attention", "processing", "excluded")}
    rows = []
    identity_pending = 0
    region_pending = 0
    for path in sorted(set(paths)):
        video_id = video_asset_id(path)
        entry = ledger.get(video_id) if isinstance(ledger.get(video_id), dict) else {}
        decision = analysis_review_decision(entry)
        status = decision["status"]
        counts[status] = counts.get(status, 0) + 1
        if decision["identity_pending"]:
            identity_pending += 1
        ai = entry.get("ai_analysis") if isinstance(entry.get("ai_analysis"), dict) else {}
        raw = ai.get("raw") if isinstance(ai.get("raw"), dict) else {}
        content_region = str(raw.get("content_region") or "不确定")
        audio_language = str(raw.get("audio_language") or "不确定")
        region_evidence = raw.get("region_evidence") or []
        if isinstance(region_evidence, str):
            region_evidence = [region_evidence]
        if not isinstance(region_evidence, list):
            region_evidence = []
        if ai.get("status") == "done" and content_region in ("", "不确定"):
            region_pending += 1
        rows.append({
            "video_id": video_id,
            "path": path,
            "filename": os.path.basename(path),
            "status": status,
            "reason": decision["reason"],
            "confidence": decision["confidence"],
            "completeness": decision["completeness"],
            "identity_pending": decision["identity_pending"],
            "categories": list(entry.get("categories") or [])[:12],
            "performers": list(raw.get("performers") or [])[:8],
            "performer_candidates": list(raw.get("performer_candidates") or [])[:8],
            "phrase": str(raw.get("phrase") or "")[:240],
            "error": str(ai.get("error") or "")[:400],
            "ai_status": str(ai.get("status") or "missing"),
            "audio_language": audio_language,
            "content_region": content_region,
            "region_confidence": _analysis_confidence(raw.get("region_confidence")),
            "region_evidence": region_evidence[:5],
            "rating": entry.get("rating"),
        })
    priority = {"attention": 0, "review": 1, "processing": 2, "auto_accepted": 3, "accepted": 4, "excluded": 5}
    rows.sort(key=lambda row: (priority.get(row["status"], 9), row["filename"].lower()))
    resolved = counts.get("auto_accepted", 0) + counts.get("accepted", 0) + counts.get("excluded", 0)
    return {
        "ok": True,
        "total": len(rows),
        "resolved": resolved,
        "needs_review": counts.get("review", 0) + counts.get("attention", 0),
        "identity_pending": identity_pending,
        "region_pending": region_pending,
        "counts": counts,
        "thresholds": {
            "auto_accept": ANALYSIS_AUTO_ACCEPT_CONFIDENCE,
            "review": ANALYSIS_REVIEW_CONFIDENCE,
        },
        "rows": rows,
    }


def apply_analysis_review_action(video_ids: list, action: str) -> dict:
    ids = list(dict.fromkeys(str(x).lower() for x in (video_ids or []) if re.fullmatch(r"[0-9a-fA-F]{16}", str(x))))
    if action not in ("accept", "exclude", "retry"):
        return {"ok": False, "error": "action must be accept, exclude or retry"}
    state = load_review_state()
    videos = state.setdefault("videos", {})
    path_by_id = {}
    for work in scanner.get_progress(0).get("works") or []:
        for item in work.get("items") or []:
            path = item.get("path")
            if item.get("type") == "video" and isinstance(path, str):
                path_by_id[video_asset_id(path)] = path
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changed = 0
    retry_paths = []
    for video_id in ids:
        entry = videos.get(video_id)
        if not isinstance(entry, dict):
            path = path_by_id.get(video_id)
            if not path:
                continue
            entry = {"path": os.path.realpath(path)}
        if action == "retry":
            path = entry.get("path")
            if not isinstance(path, str) or not os.path.isfile(path) or not is_path_under_root(path):
                continue
            entry.pop("analysis_review", None)
            entry["ai_analysis"] = {"status": "queued", "schema_version": 4, "queued_at": now}
            retry_paths.append(path)
        else:
            entry["analysis_review"] = {
                "status": "accepted" if action == "accept" else "excluded",
                "updated_at": now,
            }
        videos[video_id] = entry
        changed += 1
    if changed:
        save_review_state(state)
    if retry_paths:
        threading.Thread(target=_resume_analysis_after_active_batch, args=(get_scan_root(),), daemon=True).start()
    return {"ok": True, "changed": changed, "retry_count": len(retry_paths)}


def clear_review_state(scan_root: str | None = None) -> None:
    path = _review_state_store_path(scan_root)
    with _review_state_lock:
        if os.path.isfile(path):
            os.remove(path)


def import_review_tags(tags: dict) -> dict:
    """合并 {work_id: kept|pending} 到当前 scan_root 审阅账本（仅填空 tag）。"""
    state = load_review_state()
    works = state.setdefault("works", {})
    imported = 0
    for wid, tag in (tags or {}).items():
        if not isinstance(wid, str) or tag not in ("kept", "pending", "deleted"):
            continue
        ent = dict(works.get(wid) or {})
        if ent.get("tag") in ("kept", "pending", "deleted"):
            continue
        ent["tag"] = tag
        ent["tag_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        works[wid] = ent
        imported += 1
    save_review_state(state)
    return {"ok": True, "imported": imported, "state": state}


def ensure_placeholder(dst: str):
    if not os.path.exists(dst) or os.path.getsize(dst) < 100:
        try:
            import shutil
            shutil.copy(PLACEHOLDER, dst)
        except Exception:
            pass


def open_in_file_manager(path: str) -> bool:
    """Cross-platform 'reveal in file manager' helper."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], timeout=10, capture_output=True)
        elif os.name == "nt":
            # explorer can open both files and folders
            subprocess.run(["explorer", path], timeout=10)
        else:
            subprocess.run(["xdg-open", path], timeout=10)
        return True
    except Exception:
        return False


def remove_media_thumb_cache(media_path: str) -> None:
    """删除该媒体在缓存目录下对应的缩略图文件夹（与 generate_* 使用的 sha256(abspath) 一致）。"""
    try:
        key = sha256_str(os.path.abspath(media_path))
        thumb_dir = _thumb_cache_dir(key)
        if os.path.isdir(thumb_dir):
            shutil.rmtree(thumb_dir, ignore_errors=True)
    except Exception:
        pass


def remove_media_play_cache(media_path: str) -> None:
    """Remove the current play-ready cache for a source before deleting it."""
    try:
        cached = play_cache_path(media_path)
        if os.path.isfile(cached):
            os.remove(cached)
        parent = os.path.dirname(cached)
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except Exception:
        pass


_active_ffmpeg_procs: dict[str, set[subprocess.Popen]] = {}
_active_ffmpeg_procs_lock = threading.Lock()


def _media_path_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _register_ffmpeg_proc(path: str, proc: subprocess.Popen) -> None:
    key = _media_path_key(path)
    with _active_ffmpeg_procs_lock:
        _active_ffmpeg_procs.setdefault(key, set()).add(proc)


def _unregister_ffmpeg_proc(path: str, proc: subprocess.Popen) -> None:
    key = _media_path_key(path)
    with _active_ffmpeg_procs_lock:
        procs = _active_ffmpeg_procs.get(key)
        if not procs:
            return
        procs.discard(proc)
        if not procs:
            _active_ffmpeg_procs.pop(key, None)


def release_media_resources(path: str) -> int:
    """Stop live transcodes reading path so Windows can delete the media file."""
    key = _media_path_key(path)
    with _active_ffmpeg_procs_lock:
        procs = list(_active_ffmpeg_procs.pop(key, set()))
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    return len(procs)


def _safe_remove(path: str) -> None:
    """Release active readers before remove; retry transient Windows locks."""
    release_media_resources(path)
    remove_media_play_cache(path)
    delays = (0.0, 0.3, 0.5) if sys.platform == "win32" else (0.0,)
    for idx, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            os.remove(path)
            return
        except PermissionError:
            if idx == len(delays) - 1:
                raise


_delete_trash_lock = threading.Lock()


def _delete_trash_store_path() -> str:
    """按当前扫描根隔离清单，避免换卷后误删。"""
    return os.path.join(CACHE_DIR, f"delete_trash_{sha256_str(os.path.abspath(get_scan_root()))}.json")


def _delete_trash_load_unlocked() -> list:
    p = _delete_trash_store_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def _delete_trash_save_unlocked(items: list) -> None:
    p = _delete_trash_store_path()
    tmp = p + ".tmp"
    payload = {
        "items": items,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _delete_trash_prune_unlocked(items: list) -> list:
    out = []
    for it in items:
        path = it.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        try:
            if not os.path.isfile(path):
                continue
            if not is_path_under_root(path):
                continue
        except OSError:
            continue
        out.append(it)
    return out


def delete_trash_list() -> list:
    """返回当前扫描根下仍存在的待删文件条目（顺带落盘剔除已消失路径）。"""
    with _delete_trash_lock:
        raw = _delete_trash_load_unlocked()
        pruned = _delete_trash_prune_unlocked(raw)
        if len(pruned) != len(raw):
            _delete_trash_save_unlocked(pruned)
        return [dict(x) for x in pruned]


def delete_trash_add(path: str, error: str) -> int:
    """删除失败时写入清单；同路径更新 last_error。返回当前清单长度。"""
    now = datetime.now().isoformat(timespec="seconds")
    err = (error or "")[:2000]
    with _delete_trash_lock:
        items = _delete_trash_prune_unlocked(_delete_trash_load_unlocked())
        found = False
        for it in items:
            if it.get("path") == path:
                it["last_error"] = err
                it["fail_count"] = int(it.get("fail_count") or 0) + 1
                it["last_failed_at"] = now
                found = True
                break
        if not found:
            items.append(
                {
                    "path": path,
                    "added_at": now,
                    "last_error": err,
                    "fail_count": 1,
                    "last_failed_at": now,
                }
            )
        _delete_trash_save_unlocked(items)
        return len(items)


def _delete_trash_path_norm(p: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(p))
    except Exception:
        return p


def delete_trash_remove_paths(paths: list) -> int:
    """从清单移除（不删磁盘文件）。返回剩余条数。"""
    want = set()
    for p in paths or []:
        if isinstance(p, str) and p:
            want.add(_delete_trash_path_norm(p))
    if not want:
        return len(delete_trash_list())
    with _delete_trash_lock:
        items = _delete_trash_prune_unlocked(_delete_trash_load_unlocked())
        items = [it for it in items if _delete_trash_path_norm(it.get("path", "")) not in want]
        _delete_trash_save_unlocked(items)
        return len(items)


def delete_trash_clear() -> None:
    with _delete_trash_lock:
        _delete_trash_save_unlocked([])


def delete_trash_retry_all() -> dict:
    """依次重试删除清单内全部文件；成功则移除并清缩略图缓存。"""
    now = datetime.now().isoformat(timespec="seconds")
    remaining: list = []
    deleted = 0
    errors: list = []
    with _delete_trash_lock:
        items = _delete_trash_prune_unlocked(_delete_trash_load_unlocked())
        for it in items:
            p = it.get("path")
            if not isinstance(p, str) or not p:
                continue
            if not os.path.isfile(p) or not is_path_under_root(p):
                continue
            try:
                _safe_remove(p)
                remove_media_thumb_cache(p)
                deleted += 1
            except Exception as e:
                err = str(e)
                it["last_error"] = err[:2000]
                it["fail_count"] = int(it.get("fail_count") or 0) + 1
                it["last_failed_at"] = now
                remaining.append(it)
                errors.append({"path": p, "error": err})
        _delete_trash_save_unlocked(remaining)
    return {"ok": True, "deleted": deleted, "remaining": len(remaining), "errors": errors}


def delete_trash_delete_selected(paths: list) -> dict:
    """仅删除「当前废纸篓队列」中出现的路径（子集），用于前端多选批量删。"""
    raw: list[str] = []
    for p in paths or []:
        if isinstance(p, str) and p.strip():
            raw.append(p.strip())
    seen: set[str] = set()
    unique_req: list[str] = []
    for p in raw:
        k = _delete_trash_path_norm(p)
        if k in seen:
            continue
        seen.add(k)
        unique_req.append(p)

    skipped: list = []
    errors: list = []
    deleted = 0
    now = datetime.now().isoformat(timespec="seconds")

    with _delete_trash_lock:
        items = _delete_trash_prune_unlocked(_delete_trash_load_unlocked())
        for req_path in unique_req:
            req_n = _delete_trash_path_norm(req_path)
            found = False
            for idx, it in enumerate(items):
                p = it.get("path")
                if not isinstance(p, str) or _delete_trash_path_norm(p) != req_n:
                    continue
                found = True
                canon = p
                if not os.path.isfile(canon) or not is_path_under_root(canon):
                    skipped.append({"path": req_path, "error": "file_missing"})
                    items.pop(idx)
                    break
                try:
                    _safe_remove(canon)
                    remove_media_thumb_cache(canon)
                    deleted += 1
                    items.pop(idx)
                except Exception as e:
                    err = str(e)
                    it["last_error"] = err[:2000]
                    it["fail_count"] = int(it.get("fail_count") or 0) + 1
                    it["last_failed_at"] = now
                    errors.append({"path": canon, "error": err})
                break
            if not found:
                skipped.append({"path": req_path, "error": "not_in_trash_queue"})
        _delete_trash_save_unlocked(items)
        remaining = len(items)

    return {
        "ok": True,
        "deleted": deleted,
        "remaining": remaining,
        "errors": errors,
        "skipped": skipped,
    }


def _path_under_work_dir(file_path: str, work_path: str) -> bool:
    try:
        wp = os.path.realpath(work_path)
        fp = os.path.realpath(file_path)
    except OSError:
        return False
    if fp == wp:
        return os.path.isfile(fp)
    return fp.startswith(wp + os.sep)


def _can_remove_work_folder_dir(work_path: str) -> bool:
    """根目录平铺作品（path=扫描根）不删除文件夹本身。"""
    try:
        root = os.path.realpath(get_scan_root())
        wp = os.path.realpath(work_path)
    except OSError:
        return False
    if wp == root:
        return False
    return wp.startswith(root + os.sep) and os.path.isdir(wp)


def try_remove_empty_work_folder(work_path: str) -> bool:
    """媒体删光后，自底向上移除空子目录并尝试删除作品文件夹（A+B 之 B）。"""
    if not _can_remove_work_folder_dir(work_path):
        return False
    wp = os.path.realpath(work_path)
    removed_top = False
    try:
        for dirpath, _dirnames, _filenames in os.walk(wp, topdown=False):
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
                    if os.path.normcase(dirpath) == os.path.normcase(wp):
                        removed_top = True
            except OSError:
                pass
        if not removed_top and os.path.isdir(wp):
            try:
                if not os.listdir(wp):
                    os.rmdir(wp)
                    removed_top = True
            except OSError:
                pass
    except OSError:
        return False
    return removed_top


def delete_work_all_media_and_folder(work_path: str, paths: list) -> dict:
    """删除作品目录下指定媒体路径；全部成功后尝试移除已清空的作品文件夹。"""
    if not isinstance(work_path, str) or not work_path.strip():
        return {"ok": False, "error": "missing work_path"}
    try:
        wp = os.path.realpath(os.path.abspath(os.path.expanduser(work_path.strip())))
    except OSError:
        return {"ok": False, "error": "invalid work_path"}
    if not os.path.isdir(wp) or not is_path_under_root(wp):
        return {"ok": False, "error": "forbidden work_path"}

    raw_paths: list[str] = []
    seen: set[str] = set()
    for p in paths or []:
        if not isinstance(p, str) or not p.strip():
            continue
        try:
            fp = os.path.realpath(p.strip())
        except OSError:
            continue
        key = os.path.normcase(fp)
        if key in seen:
            continue
        seen.add(key)
        if not _path_under_work_dir(fp, wp):
            continue
        if not is_path_under_root(fp):
            continue
        raw_paths.append(fp)

    deleted = 0
    deleted_paths: list[str] = []
    errors: list[dict] = []
    for fp in raw_paths:
        if not os.path.isfile(fp):
            continue
        try:
            _safe_remove(fp)
            remove_media_thumb_cache(fp)
            deleted += 1
            deleted_paths.append(fp)
        except Exception as e:
            err = str(e)
            delete_trash_add(fp, err)
            errors.append({"path": fp, "error": err})

    folder_removed = False
    if not errors and _can_remove_work_folder_dir(wp):
        folder_removed = try_remove_empty_work_folder(wp)

    trash_n = len(delete_trash_list())
    return {
        "ok": True,
        "deleted": deleted,
        "deleted_paths": deleted_paths,
        "errors": errors,
        "folder_removed": folder_removed,
        "trash_count": trash_n,
    }


def get_video_info(path: str) -> dict:
    info = {"duration": 0.0, "width": 0, "height": 0, "codec": "", "bitrate": 0, "fps": 0.0}
    _probe_timeout = 90 if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical") else 45
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "error",
             "-show_entries", "format=duration,bit_rate",
             "-show_entries", "stream=width,height,codec_name,r_frame_rate",
             "-select_streams", "v:0",
             "-of", "json", path],
            capture_output=True, text=True, timeout=_probe_timeout
        )
        data = json.loads(out.stdout)
        if "format" in data:
            fmt = data["format"]
            if "duration" in fmt:
                info["duration"] = float(fmt["duration"])
            if "bit_rate" in fmt:
                info["bitrate"] = int(fmt["bit_rate"])
        if "streams" in data and len(data["streams"]) > 0:
            s = data["streams"][0]
            info["width"] = s.get("width", 0)
            info["height"] = s.get("height", 0)
            info["codec"] = s.get("codec_name", "")
            rf = s.get("r_frame_rate", "")
            if isinstance(rf, str) and "/" in rf:
                try:
                    num, den = rf.split("/")
                    info["fps"] = round(float(num) / float(den), 2)
                except Exception:
                    pass
    except Exception:
        pass
    return info


def _parse_ffprobe_datetime(raw: str) -> datetime | None:
    """Parse common ffprobe datetime tag formats into naive local datetime."""
    if not raw:
        return None
    s = str(raw).strip()
    # Common: 2020-01-02T03:04:05.000000Z or 2020-01-02 03:04:05
    s = s.replace(" ", "T")
    s = re.sub(r"\.\d+", "", s)  # drop fractional seconds
    s = s.replace("Z", "")
    # QuickTime sometimes: 2020-01-02T03:04:05+08:00
    s = re.sub(r"([+-]\d{2}:\d{2})$", "", s)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def get_video_datetime_local(path: str) -> datetime | None:
    """Best-effort capture time from metadata; fallback to mtime."""
    _probe_timeout = 90 if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical") else 45
    try:
        out = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-show_entries",
                "format_tags=creation_time:format_tags=com.apple.quicktime.creationdate",
                "-show_entries",
                "stream_tags=creation_time",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=_probe_timeout,
        )
        data = json.loads(out.stdout or "{}")
        tags = (data.get("format") or {}).get("tags") or {}
        for k in ("creation_time", "com.apple.quicktime.creationdate"):
            dt = _parse_ffprobe_datetime(tags.get(k))
            if dt:
                return dt
        for st in (data.get("streams") or []):
            stags = (st or {}).get("tags") or {}
            dt = _parse_ffprobe_datetime(stags.get("creation_time"))
            if dt:
                return dt
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except Exception:
        return None


def fmt_datetime_ymdhms(dt: datetime | None) -> str:
    if not dt:
        return ""
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def fmt_duration(sec: float) -> str:
    if sec <= 0:
        return ""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def generate_video_thumb_single(video_path: str, video_info: dict = None) -> str:
    if not os.path.isfile(video_path):
        return ""
    file_hash = sha256_str(os.path.abspath(video_path))
    thumb_dir = _thumb_cache_dir(file_hash)
    os.makedirs(thumb_dir, exist_ok=True)
    dst = os.path.join(thumb_dir, "0.jpg")
    if os.path.exists(dst) and os.path.getsize(dst) > 100:
        return dst
    info = video_info if video_info is not None else get_video_info(video_path)
    if not os.path.isfile(video_path):
        return ""
    ss = "00:00:01"
    if info["duration"] > 3:
        ss = str(info["duration"] / 3)
    hwaccel = _ffmpeg_thumb_hwaccel_args()
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", *hwaccel, "-an", "-dn", "-sn",
        "-ss", ss, "-i", video_path,
        "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-1", "-q:v", "3", dst
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0 and hwaccel:
            cmd = [
                FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                "-threads", "1", "-an", "-dn", "-sn",
                "-ss", ss, "-i", video_path,
                "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-1", "-q:v", "3", dst
            ]
            subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        pass
    ensure_placeholder(dst)
    return dst


def generate_video_thumbs(video_path: str, count: int = None, video_info: dict = None) -> list:
    if count is None:
        count = THUMB_COUNT
    if not os.path.isfile(video_path):
        return []
    file_hash = sha256_str(os.path.abspath(video_path))
    thumb_dir = _thumb_cache_dir(file_hash)
    os.makedirs(thumb_dir, exist_ok=True)

    expected = [os.path.join(thumb_dir, f"{i}.jpg") for i in range(count)]
    if all(os.path.exists(p) and os.path.getsize(p) > 100 for p in expected):
        return expected

    info = video_info if video_info is not None else get_video_info(video_path)
    duration = info["duration"]

    for i in range(count):
        if not os.path.isfile(video_path):
            return [p for p in expected if os.path.exists(p) and os.path.getsize(p) > 100]
        dst = expected[i]
        if os.path.exists(dst) and os.path.getsize(dst) > 100:
            continue

        if duration and duration > 1:
            ss = duration * (i + 1) / (count + 1)
        else:
            ss = 1

        hwaccel = _ffmpeg_thumb_hwaccel_args()
        cmd = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-threads", "1", *hwaccel, "-an", "-dn", "-sn",
            "-ss", str(ss), "-i", video_path,
            "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-1",
            "-c:v", "mjpeg", "-strict", "unofficial",
            "-q:v", "3", dst
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0 and hwaccel:
                cmd = [
                    FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                    "-threads", "1", "-an", "-dn", "-sn",
                    "-ss", str(ss), "-i", video_path,
                    "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-1",
                    "-c:v", "mjpeg", "-strict", "unofficial",
                    "-q:v", "3", dst
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
                err = result.stderr.decode()[:200] if result.stderr else "unknown"
                logger.warning("ffmpeg thumb error %s @ %.1fs: %s", video_path, ss, err)
        except Exception as e:
            logger.warning("ffmpeg thumb exception %s @ %.1fs: %s", video_path, ss, e)

        if not os.path.exists(dst) or os.path.getsize(dst) < 100:
            prev = expected[i - 1] if i > 0 else None
            if prev and os.path.exists(prev) and os.path.getsize(prev) > 100:
                shutil.copy(prev, dst)
            else:
                ensure_placeholder(dst)

    return expected


def generate_image_thumb(image_path: str) -> str:
    if not os.path.isfile(image_path):
        return ""
    file_hash = sha256_str(os.path.abspath(image_path))
    thumb_dir = _thumb_cache_dir(file_hash)
    os.makedirs(thumb_dir, exist_ok=True)
    dst = os.path.join(thumb_dir, "0.jpg")
    if os.path.exists(dst) and os.path.getsize(dst) > 100:
        return dst
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-an", "-dn", "-sn",
        "-i", image_path,
        "-vf", f"scale={THUMB_WIDTH}:-1",
        "-frames:v", "1", "-q:v", "3", dst
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception:
        pass
    ensure_placeholder(dst)
    return dst


# ===================== 扫描器 =====================
class MediaScanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.works = []
        self.pending_works = []
        self._pending_root_files = []
        self.scanned_dirs = 0
        self.total_dirs = 0
        self.done = False
        self.enum_error = None
        self.thread = None
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self.started = False
        self.idle = False

    def mark_idle(self):
        """preset 模式启动时不自动扫描，等待用户在页内选择媒体库。"""
        with self.lock:
            self.idle = True
            self.started = False
            self.done = True
            self.works = []
            self.pending_works = []
            self._pending_root_files = []
            self.scanned_dirs = 0
            self.total_dirs = 0
            self.enum_error = None

    def start(self):
        self.idle = False
        self.started = True
        self.done = False
        load_library_index()
        self._enumerate()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _enumerate_deep_fallback(self, candidates: list, root_flat: list) -> None:
        """一级目录未发现媒体时沿整棵树查找，安全跟随根内符号链接。"""
        scan_root = os.path.realpath(get_scan_root())
        tops_found = set()
        seen = {os.path.normcase(scan_root)}
        try:
            for wroot, dirs, files in os.walk(scan_root, followlinks=True):
                _prune_walk_dirs(dirs, wroot, scan_root, seen)
                for f in files:
                    if f.startswith("._"):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
                        continue
                    fp = os.path.join(wroot, f)
                    if not is_path_under_root(fp):
                        continue
                    rel = os.path.relpath(fp, scan_root)
                    parts = rel.split(os.sep)
                    if len(parts) == 1:
                        root_flat.append(fp)
                    else:
                        tops_found.add(parts[0])
            for top in sorted(tops_found):
                wp = os.path.join(scan_root, top)
                if os.path.isdir(wp) and wp not in candidates:
                    candidates.append(wp)
        except Exception as e:
            logger.warning("深层兜底枚举错误: %s", e)
            if not self.enum_error:
                self.enum_error = str(e)

    def _enumerate(self):
        candidates = []
        root_flat = []
        self.enum_error = None
        try:
            for entry in os.scandir(get_scan_root()):
                if entry.is_file():
                    if not is_path_under_root(entry.path):
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in VIDEO_EXTS or ext in IMAGE_EXTS:
                        root_flat.append(entry.path)
                    continue
                if not entry.is_dir():
                    continue
                if entry.name in _SKIP_SCAN_SUBDIR_NAMES:
                    continue
                if not is_path_under_root(entry.path):
                    continue
                candidates.append(entry.path)
        except Exception as e:
            logger.warning("枚举错误: %s", e)
            self.enum_error = str(e)
        if not candidates and not root_flat:
            self._enumerate_deep_fallback(candidates, root_flat)
            if candidates or root_flat:
                logger.info(
                    "深层兜底: 子文件夹 %s 个，根目录媒体文件 %s 个",
                    len(candidates),
                    len(root_flat),
                )
        self._pending_root_files = sorted(root_flat)
        self.pending_works = sorted(candidates)
        extra = 1 if root_flat else 0
        self.total_dirs = len(self.pending_works) + extra
        msg = f"[枚举完成] 子文件夹作品 {len(candidates)} 个"
        if root_flat:
            msg += f"，根目录平铺媒体 {len(root_flat)} 个（将单独显示为一个作品）"
        logger.info(msg)

    def _run(self):
        futures = {}
        if self._pending_root_files:
            fut = self._executor.submit(self._process_root_flat)
            futures[fut] = "<根目录平铺媒体>"
        for work_path in self.pending_works:
            fut = self._executor.submit(self._process_work, work_path)
            futures[fut] = work_path

        for future in as_completed(futures):
            label = futures[future]
            try:
                work = future.result(timeout=600)
                if work:
                    with self.lock:
                        self.works.append(work)
                self.scanned_dirs += 1
                save_library_index()
            except Exception as e:
                logger.error("扫描错误 %s: %s", label, e)
                self.scanned_dirs += 1
                save_library_index()

        self.done = True
        save_library_index()
        logger.info("扫描完成")

    def _build_work_from_items(self, items, work_path: str, display_name_override: str = None):
        if not items:
            return None
        items.sort(key=lambda x: (0 if x["type"] == "video" else 1, x["name"]))
        video_items = []
        image_items = []
        valid_items = []
        mtime = 0
        for it in items:
            if not os.path.isfile(it["path"]):
                continue
            try:
                mtime = max(mtime, os.path.getmtime(it["path"]))
            except Exception:
                pass
            if it["type"] == "video":
                it["asset_id"] = video_asset_id(it["path"])
                info = get_video_info(it["path"])
                it["duration"] = info["duration"]
                it["width"] = info["width"]
                it["height"] = info["height"]
                it["codec"] = info.get("codec", "")
                it["bitrate"] = info.get("bitrate", 0)
                it["fps"] = info.get("fps", 0.0)
                if not info["duration"] or not info["width"] or not info["height"]:
                    it["invalid_reason"] = "无法读取视频信息"
                elif info["duration"] < 3:
                    it["invalid_reason"] = "视频时长不足 3 秒"
                # 只 ffprobe 一次；此前每条视频会 probe 最多 3 次，NAS/机械盘上极慢且重复读头
                it["thumb"] = generate_video_thumb_single(it["path"], info)
                it["thumbs"] = generate_video_thumbs(
                    it["path"], count=THUMB_COUNT, video_info=info
                )
                video_items.append(it)
            else:
                it["thumb"] = generate_image_thumb(it["path"])
                it["thumbs"] = [it["thumb"]]
                image_items.append(it)
            valid_items.append(it)

        items = valid_items
        if not items:
            return None

        main_video = max(video_items, key=lambda x: x["size"]) if video_items else None
        main_duration = main_video.get("duration", 0) if main_video else 0
        
        video_ids = sorted(it["asset_id"] for it in video_items if "asset_id" in it)
        if video_ids:
            id_seed = "\n".join(video_ids)
        else:
            id_seed = os.path.abspath(work_path) + ("\n#root_flat" if display_name_override else "")
        work_id = sha256_str(id_seed)
        
        total_size = sum(it["size"] for it in items)
        total_duration = sum(it.get("duration", 0) for it in video_items)
        invalid_count = sum(1 for it in video_items if it.get("invalid_reason"))
        if display_name_override:
            display_name = display_name_override
        else:
            display_name = html_mod.unescape(os.path.basename(work_path))
        return {
            "id": work_id,
            "name": display_name,
            "path": work_path,
            "items": items,
            "thumbs": [],
            "video_count": len(video_items),
            "image_count": len(image_items),
            "total_size": total_size,
            "total_duration": total_duration,
            "main_duration": main_duration,
            "mtime": mtime,
            "invalid_count": invalid_count,
            "all_invalid": bool(video_items) and invalid_count == len(video_items) and not image_items,
        }

    def _process_root_flat(self):
        try:
            paths = sorted(self._pending_root_files)
            if not paths:
                return None
            items = []
            for fpath in paths:
                if not is_path_under_root(fpath):
                    continue
                f = os.path.basename(fpath)
                if f.startswith("._"):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext in VIDEO_EXTS:
                    items.append({
                        "type": "video",
                        "path": fpath,
                        "name": f,
                        "size": os.path.getsize(fpath),
                    })
                elif ext in IMAGE_EXTS:
                    items.append({
                        "type": "image",
                        "path": fpath,
                        "name": f,
                        "size": os.path.getsize(fpath),
                    })
            return self._build_work_from_items(
                items,
                get_scan_root(),
                "根目录内的媒体（未放入子文件夹）",
            )
        except Exception as e:
            logger.warning("process root_flat error: %s", e)
            return None

    def _process_work(self, work_path: str):
        try:
            if not is_path_under_root(work_path):
                return None
            items = []
            items_for_hash = []
            scan_root = os.path.realpath(get_scan_root())
            seen = {os.path.normcase(os.path.realpath(work_path))}
            for root, dirs, files in os.walk(work_path, followlinks=True):
                _prune_walk_dirs(dirs, root, scan_root, seen)
                for f in files:
                    if f.startswith("._"):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
                        continue
                    fpath = os.path.join(root, f)
                    if not is_path_under_root(fpath):
                        continue
                    try:
                        st = os.stat(fpath)
                        fsize = st.st_size
                        mtime = st.st_mtime
                    except OSError:
                        continue
                        
                    items.append({
                        "type": "video" if ext in VIDEO_EXTS else "image",
                        "path": fpath,
                        "name": f,
                        "size": fsize,
                        "mtime": mtime
                    })
                    items_for_hash.append(f"{fpath}|{fsize}|{mtime}")
            
            if not items:
                return None
            items_for_hash.sort()
            sig = sha256_str("\n".join(items_for_hash))
            
            work_id_seed = os.path.abspath(work_path)
            work_id = sha256_str(work_id_seed)
            
            cached_work = _library_index_cache.get(work_id)
            if cached_work and cached_work.get("_signature") == sig:
                return cached_work
                
            work = self._build_work_from_items(items, work_path, None)
            if work:
                work["_signature"] = sig
                with _library_index_lock:
                    _library_index_cache[work_id] = work
            return work
        except Exception as e:
            logger.warning("process error %s: %s", work_path, e)
            return None

    def get_progress(self, since: int = 0):
        with self.lock:
            if self.idle and not self.started:
                body = {
                    "scanned": 0,
                    "total": 0,
                    "done": True,
                    "idle": True,
                    "works": [],
                    "next_since": 0,
                    "enum_error": None,
                    "scan_root": get_scan_root(),
                }
            else:
                body = {
                    "scanned": self.scanned_dirs,
                    "total": self.total_dirs,
                    "done": self.done,
                    "idle": False,
                    "works": self.works[since:],
                    "next_since": len(self.works),
                    "enum_error": self.enum_error,
                    "scan_root": get_scan_root(),
                }
            if scan_presets_enabled():
                body["scan_presets"] = get_scan_presets()
            return body


def _normalize_scan_path_input(s: str) -> str:
    """去掉首尾空白，并把弯引号等换成 ASCII，避免粘贴路径不可见字符导致目录判断失败。"""
    t = (s or "").strip()
    for a, b in (
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\ufeff", ""),
        ("\u200b", ""),
        ("\u200c", ""),
        ("\u200d", ""),
        ("\u2060", ""),
    ):
        t = t.replace(a, b)
    return t.strip().strip('"').strip("'")


def _check_scan_root_directory(p: str) -> tuple[bool, str | None]:
    """判断路径是否为可访问的目录（含 macOS /Volumes NAS 自动挂载触发）。"""
    try:
        if os.path.isdir(p):
            return True, None
        if sys.platform == "darwin" and p.startswith("/Volumes/"):
            try:
                os.listdir(p)
            except OSError as e:
                errno = getattr(e, "errno", None)
                if errno == 2:
                    return (
                        False,
                        f"未找到「{p}」。请先用 Finder（⌘K）连接服务器并挂载 NAS，"
                        "再在「扫描根目录」填写 /Volumes/下的文件夹路径。",
                    )
                if errno == 13:
                    return False, f"没有权限访问「{p}」。请在系统设置中允许本应用访问该磁盘。"
                return False, f"无法访问「{p}」: {e}"
            if os.path.isdir(p):
                return True, None
        if os.path.lexists(p) and os.path.isfile(p):
            return False, "请选择文件夹路径，而不是单个文件。"
        return False, "路径不存在或不是文件夹。"
    except OSError as e:
        return False, f"无法访问路径: {e}"


def resolve_scan_root_path(raw: str) -> tuple[str | None, str | None]:
    """解析用户输入的扫描根路径。返回 (绝对路径, 错误说明)。"""
    t = _normalize_scan_path_input(raw)
    if not t:
        return None, "请输入目录路径"

    low = t.lower()
    if low.startswith(("smb://", "afp://", "nfs://", "ftp://", "cifs://")):
        return (
            None,
            "这是网络地址，不能直接作为扫描根目录。请先用 Finder（⌘K）挂载 NAS，"
            "再填写本机路径，例如 /Volumes/你的共享名/子文件夹",
        )
    if low.startswith("\\\\"):
        return (
            None,
            "这是 Windows 网络路径格式。在 Mac 上请先用 Finder（⌘K）挂载，"
            "再使用 /Volumes/… 路径。",
        )

    if t.startswith("file://"):
        try:
            from urllib.parse import unquote, urlparse

            t = unquote(urlparse(t).path)
        except Exception:
            return None, "无法解析 file:// 路径"

    if os.sep == "/" and "\\" in t and not re.match(r"^[A-Za-z]:\\", t):
        t = t.replace("\\", "/")

    t = os.path.expanduser(t)
    if not os.path.isabs(t):
        t = os.path.abspath(t)

    candidates: list[str] = []
    seen: set[str] = set()
    for cand in (t, os.path.abspath(t)):
        try:
            rp = os.path.realpath(cand)
        except OSError:
            rp = os.path.abspath(cand)
        for p in (rp, os.path.abspath(cand)):
            key = os.path.normcase(p)
            if key not in seen:
                seen.add(key)
                candidates.append(p)

    last_err: str | None = None
    for p in candidates:
        ok, err = _check_scan_root_directory(p)
        if ok:
            return p, None
        last_err = err
    return None, last_err or "路径不存在或不是文件夹"


def resolve_cache_dir_path(raw: str) -> tuple[str | None, str | None]:
    t = _normalize_scan_path_input(raw)
    if not t:
        return None, "请输入缓存目录路径"
    if t.startswith("file://"):
        try:
            from urllib.parse import unquote, urlparse

            t = unquote(urlparse(t).path)
        except Exception:
            return None, "无法解析 file:// 路径"
    if os.sep == "/" and "\\" in t and not re.match(r"^[A-Za-z]:\\", t):
        t = t.replace("\\", "/")
    t = os.path.expanduser(t)
    if not os.path.isabs(t):
        t = os.path.abspath(t)
    try:
        p = os.path.realpath(os.path.abspath(t))
    except OSError:
        p = os.path.abspath(t)
    if os.path.lexists(p) and not os.path.isdir(p):
        return None, "缓存路径必须是文件夹，不能是单个文件"
    try:
        os.makedirs(p, exist_ok=True)
        probe = os.path.join(p, f".mb_cache_write_test_{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:
        return None, f"缓存目录不可写: {e}"
    return p, None


def cache_settings_payload() -> dict:
    return {
        "ok": True,
        "cache_dir": CACHE_DIR,
        "settings_path": SETTINGS_PATH,
        "env_override": bool((os.environ.get("MB_CACHE_DIR") or "").strip()),
        "cache": _cache_dir_stats(CACHE_DIR),
    }


def replace_cache_dir(new_dir: str) -> tuple[bool, dict]:
    global CACHE_DIR, scanner
    p, err = resolve_cache_dir_path(new_dir)
    if not p:
        return False, {"ok": False, "error": err or "缓存目录无效"}
    old_cache = CACHE_DIR
    CACHE_DIR = p
    _ensure_cache_dir_ready()
    _PERSISTENT_SETTINGS["cache_dir"] = p
    try:
        _save_persistent_settings(_PERSISTENT_SETTINGS)
    except Exception as e:
        CACHE_DIR = old_cache
        _ensure_cache_dir_ready()
        return False, {"ok": False, "error": f"保存设置失败: {e}"}
    logger.info("缓存目录切换为: %s", CACHE_DIR)
    old = scanner
    scanner = MediaScanner()
    scanner.start()
    if "resume_pending_video_analysis" in globals():
        threading.Thread(
            target=resume_pending_video_analysis,
            args=(_scan_root,),
            daemon=True,
        ).start()
    try:
        old._executor.shutdown(wait=False)
    except Exception:
        pass
    return True, cache_settings_payload()


def replace_scan_root(new_root: str, persist: bool = False) -> bool:
    """切换扫描根目录并启动新扫描任务。返回是否成功。"""
    global _scan_root, scanner
    p, err = resolve_scan_root_path(new_root)
    if not p:
        if err:
            logger.warning("扫描根目录无效: %s", err)
        return False
    if scan_presets_enabled() and not is_scan_root_in_presets(p):
        logger.warning("扫描根不在 MB_SCAN_PRESETS 白名单: %s", p)
        return False
    _scan_root = p
    if persist and not (os.environ.get("MB_ROOT_DIR") or "").strip() and not scan_presets_enabled():
        _PERSISTENT_SETTINGS["scan_root"] = p
        try:
            _save_persistent_settings(_PERSISTENT_SETTINGS)
        except Exception as e:
            logger.warning("保存扫描目录设置失败: %s", e)
    prof = _apply_perf_profile_for_scan_root(_scan_root)
    logger.info(
        "扫描目录切换为: %s | 自动性能档位: %s，扫描并发=%s，每视频条带缩略图=%s",
        _scan_root,
        prof,
        MAX_WORKERS,
        THUMB_COUNT,
    )
    old = scanner
    scanner = MediaScanner()
    scanner.start()
    if persist and "resume_pending_video_analysis" in globals():
        threading.Thread(
            target=resume_pending_video_analysis,
            args=(_scan_root,),
            daemon=True,
        ).start()
    try:
        old._executor.shutdown(wait=False)
    except Exception:
        pass
    return True


_SCAN_PRESETS: list[dict] = []


def parse_scan_presets_env() -> list[tuple[str, str]]:
    raw = os.environ.get("MB_SCAN_PRESETS", "").strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            path_part, label = part.split("|", 1)
        else:
            path_part, label = part, os.path.basename(part.rstrip("/")) or part
        path_part = path_part.strip()
        label = (label or path_part).strip()
        if path_part:
            out.append((path_part, label))
    return out


def build_scan_presets() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for raw_path, label in parse_scan_presets_env():
        p, err = resolve_scan_root_path(raw_path)
        if not p:
            logger.warning("MB_SCAN_PRESETS 无效项「%s」: %s", raw_path, err or "路径不可访问")
            continue
        key = os.path.normcase(os.path.realpath(p))
        if key in seen:
            continue
        seen.add(key)
        items.append({"path": p, "label": label or p})
    return items


def scan_presets_enabled() -> bool:
    return bool(_SCAN_PRESETS)


def get_scan_presets() -> list[dict]:
    return list(_SCAN_PRESETS)


def is_scan_root_in_presets(path: str) -> bool:
    if not scan_presets_enabled():
        return True
    try:
        rp = os.path.realpath(path)
    except OSError:
        return False
    for item in _SCAN_PRESETS:
        try:
            if os.path.realpath(item["path"]) == rp:
                return True
        except OSError:
            continue
    return False


def preset_reject_reason(raw: str) -> str | None:
    if not scan_presets_enabled():
        return None
    p, err = resolve_scan_root_path(raw)
    if not p:
        return err or "路径不存在或不是文件夹"
    if not is_scan_root_in_presets(p):
        return "该路径不在已配置的媒体库列表中"
    return None


def should_auto_scan_on_startup() -> bool:
    if scan_presets_enabled():
        return os.environ.get("MB_AUTO_SCAN", "").strip().lower() in (
            "1",
            "yes",
            "true",
            "on",
        )
    return True


def bootstrap_scan_configuration() -> None:
    global _scan_root, _SCAN_PRESETS
    _SCAN_PRESETS = build_scan_presets()
    if scan_presets_enabled():
        env_root = os.environ.get("MB_ROOT_DIR") or ROOT_DIR
        p, _ = resolve_scan_root_path(str(env_root))
        allowed = {os.path.realpath(x["path"]) for x in _SCAN_PRESETS}
        if p and os.path.realpath(p) in allowed:
            _scan_root = p
        else:
            _scan_root = _SCAN_PRESETS[0]["path"]
            if p:
                logger.warning(
                    "MB_ROOT_DIR=%s 不在 MB_SCAN_PRESETS 中，已使用 %s",
                    env_root,
                    _scan_root,
                )
        logger.info(
            "媒体库 preset 共 %s 个：%s",
            len(_SCAN_PRESETS),
            "；".join(f"{x['label']}({x['path']})" for x in _SCAN_PRESETS),
        )
    else:
        _scan_root = os.path.realpath(os.path.abspath(os.path.expanduser(ROOT_DIR)))


bootstrap_scan_configuration()


scanner = MediaScanner()


def _tool_version_ok(bin_path: str) -> tuple[bool, str | None]:
    try:
        r = subprocess.run(
            [bin_path, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return True, None
        msg = (r.stderr or r.stdout or "")[:400]
        return False, msg or "nonzero exit"
    except Exception as e:
        return False, str(e)[:400]


def _cache_dir_stats(cache_dir: str, max_files: int = 5_000) -> dict:
    """Return a bounded cache sample so /health stays responsive on large libraries."""
    total_bytes = 0
    n = 0
    truncated = False
    try:
        for dp, _dns, fns in os.walk(cache_dir):
            for fn in fns:
                if n >= max_files:
                    truncated = True
                    break
                fp = os.path.join(dp, fn)
                try:
                    total_bytes += os.path.getsize(fp)
                    n += 1
                except OSError:
                    pass
            if truncated:
                break
    except Exception as e:
        return {"dir": cache_dir, "file_count": -1, "total_bytes": -1, "error": str(e)[:500]}
    out = {"dir": cache_dir, "file_count": n, "total_bytes": total_bytes}
    if truncated:
        out["truncated"] = True
    return out


def build_health_payload() -> tuple[dict, int]:
    """
    返回 (JSON 可序列化字典, HTTP 状态码)。
    503：ffmpeg/ffprobe 不可用、枚举/扫描根错误、磁盘使用率不可读或 ≥99%。
    """
    root = get_scan_root()
    disk: dict = {"path": root}
    try:
        du = shutil.disk_usage(root)
        pct = round(100.0 * du.used / du.total, 2) if du.total else None
        disk["used_percent"] = pct
        disk["free_bytes"] = du.free
        disk["total_bytes"] = du.total
    except Exception as e:
        disk["used_percent"] = None
        disk["error"] = str(e)[:500]

    prog = scanner.get_progress(0)
    if prog.get("enum_error"):
        scan_state = "error"
    elif prog.get("idle"):
        scan_state = "awaiting_scan"
    elif not prog.get("done"):
        scan_state = "scanning"
    else:
        scan_state = "idle"
    scan = {
        "state": scan_state,
        "enum_error": prog.get("enum_error"),
        "scanned": prog.get("scanned"),
        "total": prog.get("total"),
        "works_ready": len(scanner.works),
        "scan_root": get_scan_root(),
    }
    if scan_presets_enabled():
        scan["presets"] = get_scan_presets()

    fa, fe = _tool_version_ok(FFMPEG_BIN)
    pa, pe = _tool_version_ok(FFPROBE_BIN)
    ffmpeg = {"available": fa, "binary": FFMPEG_BIN, "error": fe, "hw": resolve_ffmpeg_hw()}
    ffprobe = {"available": pa, "binary": FFPROBE_BIN, "error": pe}

    oh, om, _ofr, _oto = _ollama_config()
    oc_ok, oc_err = _ollama_health_check(oh, timeout=2.5)
    ollama = {"host": oh, "model": om, "reachable": oc_ok, "error": oc_err or None}

    cache = _cache_dir_stats(CACHE_DIR)
    uptime = round(time.monotonic() - _APP_BOOT_MONOTONIC, 3)

    body: dict = {
        "ok": True,
        "version": APP_VERSION,
        "uptime_seconds": uptime,
        "disk": disk,
        "scan": scan,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "ollama": ollama,
        "cache": cache,
    }

    pct = disk.get("used_percent")
    bad_disk = pct is None or (isinstance(pct, (int, float)) and pct >= 99.0)
    unhealthy = (
        not fa
        or not pa
        or scan_state == "error"
        or bad_disk
    )
    if unhealthy:
        body["ok"] = False
        return body, 503
    return body, 200


# 由 main() 赋值；用于从浏览器请求优雅退出
_http_server = None


def _safe_name_token(text: str, fallback: str = "untitled") -> str:
    s = (text or "").strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff\-_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    return s or fallback


def _guess_scene_tags(path: str) -> list:
    parts = [p for p in os.path.normpath(path).split(os.sep) if p]
    src = " ".join(parts[-3:]).lower()
    tags = []
    for k in ("japan", "iceland", "tokyo", "osaka", "kyoto", "beijing", "shanghai", "trip", "travel"):
        if k in src:
            tags.append(k)
    return tags[:2] if tags else ["travel"]


def _build_candidate_filename(path: str, seq: int) -> str:
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    try:
        dt = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d")
    except Exception:
        dt = datetime.now().strftime("%Y%m%d")
    parent = _safe_name_token(os.path.basename(os.path.dirname(path)), "folder")
    tags = _safe_name_token("-".join(_guess_scene_tags(path)), "travel")
    # 用户决策：文件名末尾追加序号，降低同名冲突风险
    return f"{dt}_{parent}_{tags}_{seq:03d}{ext.lower()}"


def _empty_insight() -> dict:
    return {
        "llm_status": "idle",
        "time_guess": "",
        "place_guess": "",
        "event_guess": "",
        "tags": [],
        "phrase": "",
        "people_count": "不确定",
        "scene_type": "不确定",
        "production_type": "不确定",
        "camera_style": "不确定",
        "story_level": "不确定",
        "distinctive_features": [],
        "confidence": 0.0,
        "performers": [],
        "performer_candidates": [],
        "performer_evidence": {},
        "studio": "",
        "title_code": "",
        "identity_confidence": 0.0,
        "audio_language": "不确定",
        "audio_language_code": "",
        "audio_language_confidence": 0.0,
        "content_region": "不确定",
        "region_confidence": 0.0,
        "region_evidence": [],
        "user_confirmed": False,
        "confirmed_phrase": "",
        "confirmed_tags": [],
        "error": "",
    }


def _build_candidate_from_phrase(path: str, phrase: str, tags: list, seq: int) -> str:
    base = os.path.basename(path)
    _, ext = os.path.splitext(base)
    try:
        dt = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d")
    except Exception:
        dt = datetime.now().strftime("%Y%m%d")
    text = (phrase or "").strip()
    if not text:
        parts = []
        for t in (tags or [])[:10]:
            tok = _safe_name_token(str(t), "")
            if tok:
                parts.append(tok)
        text = "-".join(parts) if parts else "clip"
    token = _safe_name_token(text, "clip")
    return f"{dt}_{token}_{seq:03d}{ext.lower()}"


def _extract_llm_frames(video_path: str, out_dir: str, count: int) -> list:
    os.makedirs(out_dir, exist_ok=True)
    info = get_video_info(video_path)
    dur = float(info.get("duration") or 0)
    if dur < 0.25:
        dur = 1.0
    out_paths = []
    if count >= 4:
        title_ratio = min(0.03, 2.0 / dur)
        ratios = [title_ratio] + [0.20 + (0.70 * i / max(1, count - 2)) for i in range(count - 1)]
    else:
        ratios = [(i + 1) / (count + 1) for i in range(count)]
    for i, ratio in enumerate(ratios):
        t = dur * ratio
        outp = os.path.join(out_dir, f"f{i}.jpg")
        cmd = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-threads", "1", "-ss", str(t), "-i", video_path,
            "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "5", outp,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception:
            pass
        if os.path.isfile(outp) and os.path.getsize(outp) > 300:
            out_paths.append(outp)
    return out_paths


def _b64_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _ollama_health_check(host: str, timeout: float = 5.0) -> tuple:
    """启动分析前探测 Ollama 是否可达（/api/tags）。"""
    url = f"{host.rstrip('/')}/api/tags"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True, ""
    except Exception as e:
        return False, str(e)


def _ollama_resolve_vision_model(host: str, requested: str, timeout: float = 10.0) -> tuple[str, str]:
    """Use the configured model when installed, otherwise select an installed vision model."""
    try:
        req = Request(f"{host.rstrip('/')}/api/tags", method="GET")
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        models = payload.get("models") or []
        names = [str(row.get("name") or row.get("model") or "").strip() for row in models]
        names = [name for name in names if name]
        requested_base = requested.split(":", 1)[0].lower()
        for name in names:
            if name == requested or name.split(":", 1)[0].lower() == requested_base:
                return name, "configured"
        vision_hints = ("qwen2.5vl", "qwen2-vl", "qwen3-vl", "llava", "moondream", "minicpm-v", "bakllava")
        for name in names:
            low = name.lower()
            if any(hint in low for hint in vision_hints):
                return name, f"fallback_from:{requested}"
        return requested, "not_installed"
    except Exception as e:
        return requested, f"lookup_failed:{e}"


def _http_post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {e.code}: {err[:800]}") from e
    except URLError as e:
        raise RuntimeError(
            f"无法连接 Ollama（{url}）：请在本机运行 `ollama serve`，并已 `ollama pull` 视觉模型。详情: {e}"
        ) from e


def _parse_json_from_llm_text(text: str) -> dict:
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _identity_token(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _clean_person_names(value) -> list[str]:
    if isinstance(value, str):
        value = [x.strip() for x in re.split(r"[,，;；|/]", value) if x.strip()]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        name = str(item).strip().strip(".·-_ ")
        if 2 <= len(name) <= 80 and name not in out:
            out.append(name)
    return out[:12]


def _alias_performers_from_path(source_path: str) -> list[str]:
    """Match a user-maintained {canonical: [aliases]} library against path text."""
    alias_path = os.path.join(CACHE_DIR, "performer_aliases.json")
    try:
        with open(alias_path, encoding="utf-8") as f:
            aliases = json.load(f)
    except Exception:
        return []
    if not isinstance(aliases, dict):
        return []
    haystack = _identity_token(source_path)
    found = []
    for canonical, values in aliases.items():
        canonical = str(canonical).strip()
        candidates = [canonical] + (values if isinstance(values, list) else [values])
        if canonical and any(_identity_token(x) and _identity_token(x) in haystack for x in candidates):
            found.append(canonical)
    return found[:12]


def _normalize_llm_insight(raw: dict, source_path: str = "") -> dict:
    tags = raw.get("tags") or raw.get("标签")
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,，;；|/]", tags) if t.strip()]
    elif not isinstance(tags, list):
        tags = []
    # 严格过滤：仅保留不含英文字母的标签
    clean_tags: list[str] = []
    for x in tags:
        s = str(x).strip()
        if not s:
            continue
        if re.search(r"[A-Za-z]", s):
            continue
        clean_tags.append(s)
        if len(clean_tags) >= 12:
            break
    phrase = str(raw.get("phrase") or raw.get("短语") or raw.get("summary") or "").strip()
    # 若短语包含英文字符，则置空，让用户在前端手动用中文填写
    if re.search(r"[A-Za-z]", phrase):
        phrase = ""
    if not phrase and clean_tags:
        phrase = _tags_to_chinese_sentence(clean_tags)

    def controlled(value, choices: dict[str, tuple[str, ...]]) -> str:
        text = str(value or "").strip().lower()
        for canonical, aliases in choices.items():
            if text == canonical.lower() or any(alias.lower() in text for alias in aliases):
                return canonical
        return "不确定"

    people_count = controlled(raw.get("people_count") or raw.get("人物数量"), {
        "单人": ("单人", "一人", "1人"), "双人": ("双人", "两人", "2人"),
        "多人": ("多人", "三人", "群体", "3人", "4人"), "不确定": ("不确定", "未知"),
    })
    scene_type = controlled(raw.get("scene_type") or raw.get("场景类型"), {
        "卧室": ("卧室", "床上", "床铺"), "客厅": ("客厅", "沙发"),
        "室内其他": ("室内", "房间"), "户外": ("户外", "室外"),
        "海滩": ("海滩", "海边"), "交通工具": ("飞机", "火车", "汽车", "车厢"),
        "健身场所": ("健身房", "健身"), "影棚": ("影棚", "摄影棚", "布景"),
        "不确定": ("不确定", "未知"),
    })
    production_type = controlled(raw.get("production_type") or raw.get("制作类型"), {
        "剧情制作": ("剧情", "故事", "角色扮演"), "专业棚拍": ("专业", "棚拍", "多机位"),
        "自拍视频": ("自拍", "个人拍摄", "业余"), "网络直播": ("直播", "主播", "网络摄像头"),
        "合集剪辑": ("合集", "剪辑", "混剪"), "不确定": ("不确定", "未知"),
    })
    camera_style = controlled(raw.get("camera_style") or raw.get("镜头风格"), {
        "固定机位": ("固定", "静态机位"), "手持跟拍": ("手持", "跟拍"),
        "第一视角": ("第一视角", "主观视角"), "多机位": ("多机位", "镜头切换"),
        "不确定": ("不确定", "未知"),
    })
    story_level = controlled(raw.get("story_level") or raw.get("叙事程度"), {
        "无剧情": ("无剧情", "没有剧情"), "轻剧情": ("轻剧情", "简单剧情"),
        "强剧情": ("强剧情", "完整剧情", "明显剧情"), "不确定": ("不确定", "未知"),
    })
    distinctive = raw.get("distinctive_features") or raw.get("显著特征") or []
    if isinstance(distinctive, str):
        distinctive = [x.strip() for x in re.split(r"[,，;；|/]", distinctive) if x.strip()]
    if not isinstance(distinctive, list):
        distinctive = []
    distinctive = list(dict.fromkeys(str(x).strip() for x in distinctive if str(x).strip()))[:8]
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", raw.get("置信度", 0)) or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    filename_people = _clean_person_names(raw.get("filename_performers") or raw.get("文件名演员"))
    visible_people = _clean_person_names(raw.get("visible_text_performers") or raw.get("画面文字演员"))
    path_token = _identity_token(source_path)
    filename_people = [name for name in filename_people if _identity_token(name) in path_token]
    alias_people = _alias_performers_from_path(source_path) if source_path else []
    visible_by_token = {_identity_token(name): name for name in visible_people if _identity_token(name)}
    confirmed = list(alias_people)
    evidence = {}
    for name in alias_people:
        evidence[name] = ["本地别名库", "文件路径"]
    for name in filename_people:
        token = _identity_token(name)
        if token in visible_by_token and name not in confirmed:
            confirmed.append(name)
            evidence[name] = ["文件名", "画面文字"]
    candidates = list(dict.fromkeys(filename_people + visible_people))
    studio = str(raw.get("studio") or raw.get("制作方") or "").strip()[:100]
    title_code = str(raw.get("title_code") or raw.get("作品编号") or "").strip()[:80]
    try:
        identity_confidence = max(0.0, min(1.0, float(raw.get("identity_confidence", 0) or 0)))
    except (TypeError, ValueError):
        identity_confidence = 0.0
    return {
        "time_guess": str(raw.get("time") or raw.get("时间") or "").strip(),
        "place_guess": str(raw.get("place") or raw.get("地点") or "").strip(),
        "event_guess": str(raw.get("event") or raw.get("事件") or "").strip(),
        "tags": clean_tags,
        "phrase": phrase,
        "people_count": people_count,
        "scene_type": scene_type,
        "production_type": production_type,
        "camera_style": camera_style,
        "story_level": story_level,
        "distinctive_features": distinctive,
        "confidence": confidence,
        "performers": confirmed[:12],
        "performer_candidates": candidates[:12],
        "performer_evidence": evidence,
        "studio": studio,
        "title_code": title_code,
        "identity_confidence": identity_confidence,
    }


_audio_language_model = None
_audio_language_model_lock = threading.Lock()

AUDIO_LANGUAGE_NAMES = {
    "zh": "中文", "yue": "中文", "ja": "日语", "ko": "韩语", "ru": "俄语",
    "en": "英语", "th": "泰语", "vi": "越南语", "id": "印尼语", "ms": "马来语",
    "tl": "菲律宾语", "km": "高棉语", "lo": "老挝语", "my": "缅甸语",
}


def _get_audio_language_model():
    global _audio_language_model
    with _audio_language_model_lock:
        if _audio_language_model is None:
            try:
                from faster_whisper import WhisperModel
                _audio_language_model = WhisperModel(
                    os.environ.get("MB_WHISPER_MODEL", "tiny"),
                    device="cpu",
                    compute_type="int8",
                    local_files_only=True,
                )
            except Exception as e:
                logger.warning("本地语音语言模型不可用: %s", e)
                _audio_language_model = False
        return _audio_language_model


def _detect_audio_language(video_path: str, work_dir: str) -> dict:
    """Sample a short local audio clip and detect its spoken language."""
    if not ffprobe_has_audio(video_path):
        return {"audio_language": "无音轨", "audio_language_code": "", "audio_language_confidence": 1.0}
    model = _get_audio_language_model()
    if not model:
        return {"audio_language": "不确定", "audio_language_code": "", "audio_language_confidence": 0.0}
    os.makedirs(work_dir, exist_ok=True)
    info = get_video_info(video_path)
    duration = max(0.0, float(info.get("duration") or 0))
    sample_seconds = min(45.0, duration) if duration > 0 else 45.0
    start = max(0.0, min(duration * 0.28, max(0.0, duration - sample_seconds))) if duration > 0 else 0.0
    wav_path = os.path.join(work_dir, "language-sample.wav")
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-threads", "1",
        "-ss", str(start), "-i", video_path, "-t", str(sample_seconds), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=150)
        if proc.returncode != 0 or not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 16000:
            return {"audio_language": "无可识别语音", "audio_language_code": "", "audio_language_confidence": 0.0}
        segments, detected = model.transcribe(
            wav_path,
            beam_size=1,
            best_of=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        spoken = "".join(str(seg.text or "").strip() for seg in segments)
        code = str(getattr(detected, "language", "") or "").lower()
        probability = _analysis_confidence(getattr(detected, "language_probability", 0.0))
        meaningful = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", spoken)
        if len(meaningful) < 6 or probability < 0.40:
            return {"audio_language": "无可识别语音", "audio_language_code": code, "audio_language_confidence": probability}
        return {
            "audio_language": AUDIO_LANGUAGE_NAMES.get(code, "其他语言"),
            "audio_language_code": code,
            "audio_language_confidence": probability,
        }
    except Exception as e:
        logger.info("音频语言检测失败 %s: %s", os.path.basename(video_path), e)
        return {"audio_language": "不确定", "audio_language_code": "", "audio_language_confidence": 0.0}
    finally:
        try:
            if os.path.isfile(wav_path):
                os.remove(wav_path)
        except OSError:
            pass


def _controlled_region(value) -> str:
    text = str(value or "").strip().lower()
    choices = {
        "俄罗斯": ("俄罗斯", "俄语区", "russia", "russian"),
        "欧美": ("欧美", "欧洲", "美国", "英国", "北美", "western", "europe", "usa"),
        "国产": ("国产", "中国大陆", "中国", "华语", "mainland china"),
        "日本": ("日本", "日系", "japan", "japanese"),
        "韩国": ("韩国", "韩系", "korea", "korean"),
        "东南亚": ("东南亚", "泰国", "越南", "菲律宾", "印尼", "马来西亚", "新加坡", "southeast asia"),
        "其他": ("其他", "other"),
        "不确定": ("不确定", "未知", "unknown", ""),
    }
    for canonical, aliases in choices.items():
        if text == canonical.lower() or any(alias and alias.lower() in text for alias in aliases):
            return canonical
    return "不确定"


def _region_from_path(source_path: str) -> tuple[str, str]:
    parent = os.path.basename(os.path.dirname(source_path))
    text = f"{parent} {os.path.basename(source_path)}".lower()
    region_terms = (
        ("俄罗斯", ("俄罗斯", "俄语", "russia", "russian")),
        ("日本", ("日本", "日系", "无码", "有码", "jav", "japan")),
        ("韩国", ("韩国", "韩语", "韩系", "korea")),
        ("国产", ("国产", "大陆", "中国", "华语", "mandarin")),
        ("东南亚", ("东南亚", "泰国", "越南", "菲律宾", "印尼", "马来", "新加坡", "thailand", "vietnam")),
        ("欧美", ("欧美", "美国", "欧洲", "英国", "法国", "德国", "意大利", "西班牙", "western", "europe", "american")),
    )
    for region, terms in region_terms:
        hit = next((term for term in terms if term in text), "")
        if hit:
            return region, f"文件名或直属目录包含“{hit}”"
    return "不确定", ""


def _resolve_content_region(source_path: str, raw: dict, audio: dict) -> dict:
    path_region, path_evidence = _region_from_path(source_path)
    if path_region != "不确定":
        return {"content_region": path_region, "region_confidence": 0.92, "region_evidence": [path_evidence]}
    language = audio.get("audio_language")
    language_regions = {
        "俄语": "俄罗斯", "日语": "日本", "韩语": "韩国", "中文": "国产",
        "泰语": "东南亚", "越南语": "东南亚", "印尼语": "东南亚", "马来语": "东南亚",
        "菲律宾语": "东南亚", "高棉语": "东南亚", "老挝语": "东南亚", "缅甸语": "东南亚",
    }
    audio_confidence = _analysis_confidence(audio.get("audio_language_confidence"))
    if language in language_regions and audio_confidence >= 0.55:
        return {
            "content_region": language_regions[language],
            "region_confidence": round(min(0.90, audio_confidence), 3),
            "region_evidence": [f"音频语言识别为{language}"],
        }
    visual_region = _controlled_region(raw.get("visual_region") or raw.get("画面地区"))
    visual_evidence = raw.get("region_evidence") or raw.get("地区依据") or []
    if isinstance(visual_evidence, str):
        visual_evidence = [x.strip() for x in re.split(r"[,，;；|]", visual_evidence) if x.strip()]
    if not isinstance(visual_evidence, list):
        visual_evidence = []
    try:
        visual_confidence = _analysis_confidence(raw.get("region_confidence", 0))
    except (TypeError, ValueError):
        visual_confidence = 0.0
    if visual_region not in ("不确定", "其他") and visual_evidence and visual_confidence >= 0.55:
        return {
            "content_region": visual_region,
            "region_confidence": visual_confidence,
            "region_evidence": [str(x).strip()[:100] for x in visual_evidence if str(x).strip()][:5],
        }
    if language == "英语" and audio_confidence >= 0.60:
        return {"content_region": "欧美", "region_confidence": 0.62, "region_evidence": ["音频语言识别为英语"]}
    return {"content_region": "不确定", "region_confidence": 0.0, "region_evidence": []}


def _tags_to_chinese_sentence(tags: list[str]) -> str:
    """将标签用顿号连成一句可读中文，供缺省短语或前端展示。"""
    parts = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if not parts:
        return ""
    parts = parts[:12]
    if len(parts) == 1:
        return parts[0] + "。"
    return "、".join(parts) + "。"


def _vision_analyze_video(
    path: str,
    ollama_host: str,
    model: str,
    frame_count: int,
    frames_dir: str,
    request_timeout: int,
    after_frames_hook=None,
) -> dict:
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames = _extract_llm_frames(path, frames_dir, frame_count)
    if not frames:
        raise RuntimeError("无法从视频抽取帧（请检查 ffmpeg 与视频文件）")
    audio = _detect_audio_language(path, frames_dir)
    if callable(after_frames_hook):
        try:
            after_frames_hook()
        except Exception:
            pass
    b64_list = []
    for fp in frames:
        try:
            b64_list.append(_b64_file(fp))
        except Exception:
            continue
    if not b64_list:
        raise RuntimeError("视频帧编码失败")
    bn = os.path.basename(path)
    info = get_video_info(path)
    dur = fmt_duration(info.get("duration") or 0)
    meta_dt = get_video_datetime_local(path)
    meta_time = fmt_datetime_ymdhms(meta_dt) or ""
    instructions = (
        "你是面向中文用户的影像归档助手。用户会提供同一视频的若干代表帧（按时间顺序）。请根据画面内容推断（不要编造具体日期）。\n"
        "【语言硬性要求】除极少数全球通用专名（如「iPhone」「NASA」）外，time、place、event、tags 中每一项、以及 phrase 全文，"
        "必须使用「简体中文」表达；禁止使用英文单词、英文短语或中英混杂作为标签凑数。画面里若有英文招牌/路牌，请用中文概括含义，不要照抄英文。\n"
        "字段说明：\n"
        "time：拍摄时间，优先使用我提供的元数据时间；格式必须为「YYYY-MM-DD」或「YYYY-MM-DD HH:MM:SS」（24小时制）。"
        "如果确实无法确定日期，请输出空字符串 \"\"（不要输出「未知/大概/上午」这种）。\n"
        "place：地点用中文短词组（国家/城市/场景类型，如「城市街道」「海边」「室内展厅」）；\n"
        "event：正在发生的事，用中文短词组；\n"
        "people_count：只能是「单人、双人、多人、不确定」之一；\n"
        "scene_type：只能是「卧室、客厅、室内其他、户外、海滩、交通工具、健身场所、影棚、不确定」之一；\n"
        "production_type：只能是「剧情制作、专业棚拍、自拍视频、网络直播、合集剪辑、不确定」之一；\n"
        "camera_style：只能是「固定机位、手持跟拍、第一视角、多机位、不确定」之一；\n"
        "story_level：只能是「无剧情、轻剧情、强剧情、不确定」之一；\n"
        "distinctive_features：0～5个真正有区分度的显著特征，不要使用「亲密、激情、室内、人物」等泛化词；\n"
        "confidence：0到1之间的小数，表示对上述结构化判断的整体把握；\n"
        "filename_performers：只列出文件名或目录名中明确出现的演员/模特姓名，不要把制作方或普通单词当人名；\n"
        "visible_text_performers：只列出代表帧中文字明确显示的演员/模特姓名；严禁仅凭人脸或长相猜测身份；\n"
        "studio：文件名、路径、片头或水印中明确出现的制作方，没有证据则空字符串；\n"
        "title_code：明确出现的作品编号，没有则空字符串；\n"
        "identity_confidence：0到1的小数，仅表示姓名/制作方/编号文字证据的可信度；\n"
        "visual_region：只能是「俄罗斯、欧美、国产、日本、韩国、东南亚、其他、不确定」之一。只能根据片头片尾文字、字幕语言、明确水印、制作方或作品编号判断；严禁根据人物脸部、肤色或长相猜测国家；\n"
        "region_evidence：0～4条地区判断依据，只写画面文字、水印、制作方、编号等可核对证据；没有可靠证据就返回空数组；\n"
        "region_confidence：0到1的小数，只表示上述画面地区证据的可信度；\n"
        "tags：3～8 条具体关键词，禁止只用几乎适用于所有视频的泛化词；\n"
        "phrase：用中文把上述要点连成一句极短描述（约 8～24 字），适合作文件名主题，不要空格与\\/:*?\"<>|。\n"
        "只输出一个 JSON 对象，键名必须为 time, place, event, people_count, scene_type, production_type, camera_style, story_level, distinctive_features, confidence, filename_performers, visible_text_performers, studio, title_code, identity_confidence, visual_region, region_evidence, region_confidence, tags, phrase。不要输出其它文字或 Markdown。"
    )
    user_block = (
        f"文件名：{bn}\n"
        f"视频时长：{dur}\n"
        f"本地音频语言检测：{audio.get('audio_language', '不确定')}（置信度 {audio.get('audio_language_confidence', 0):.2f}；这是声音证据，不要改写）\n"
        f"拍摄时间（元数据/文件时间推断）：{meta_time or '(空)'}\n"
        f"共 {len(b64_list)} 张代表帧。请分析并返回 JSON。"
    )
    # Ollama：同一 user 消息里附带 images 数组（每项为原始 base64，无 data: 前缀）
    url = f"{ollama_host}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
        "messages": [
            {
                "role": "user",
                "content": instructions + "\n\n" + user_block,
                "images": b64_list,
            }
        ],
    }
    data = _http_post_json(url, payload, timeout=request_timeout)
    msg = data.get("message") or {}
    content_out = msg.get("content") or ""
    raw = _parse_json_from_llm_text(content_out)
    if not raw:
        raise RuntimeError(f"模型未返回有效 JSON：{content_out[:200]}")
    norm = _normalize_llm_insight(raw, path)
    norm.update(audio)
    norm.update(_resolve_content_region(path, raw, audio))
    # 若模型没给出可用日期，则回填本地元数据时间（到秒）
    if not (norm.get("time_guess") or "").strip():
        if meta_time:
            norm["time_guess"] = meta_time
    return norm


class AnalysisTaskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}

    def list_tasks(self):
        with self._lock:
            rows = []
            for t in self._tasks.values():
                insights = t.get("insights") or {}
                aj = t.get("analyze_job") or {}
                done_llm = sum(
                    1 for p in t["source_files"]
                    if (insights.get(p) or {}).get("llm_status") == "done"
                )
                conf = sum(
                    1 for p in t["source_files"]
                    if (insights.get(p) or {}).get("user_confirmed")
                )
                rows.append({
                    "id": t["id"],
                    "name": t["name"],
                    "status": t["status"],
                    "created_at": t["created_at"],
                    "file_count": len(t["source_files"]),
                    "preview_count": len(t["preview"]),
                    "executed_count": len(t["executed"]),
                    "mapping_file": t.get("mapping_file", ""),
                    "eval": t.get("eval", {}),
                    "analyze": {
                        "state": aj.get("state", "idle"),
                        "done": aj.get("done", 0),
                        "total": aj.get("total", 0),
                        "current_path": aj.get("current_path", ""),
                        "error": aj.get("error", ""),
                    },
                    "llm_done_count": done_llm,
                    "confirmed_count": conf,
                    "needs_confirm": done_llm > 0 and conf < done_llm,
                })
            rows.sort(key=lambda x: x["created_at"], reverse=True)
            return rows

    def get_task_detail(self, tid: str):
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            insights = task.get("insights") or {}
            insight_rows = []
            for p in task["source_files"]:
                row = dict(insights.get(p) or _empty_insight())
                row["path"] = p
                insight_rows.append(row)
            return {
                "id": task["id"],
                "name": task["name"],
                "status": task["status"],
                "created_at": task["created_at"],
                "source_files": list(task["source_files"]),
                "insight_rows": insight_rows,
                "insights": {k: dict(v) for k, v in insights.items()},
                "analyze_job": dict(task.get("analyze_job") or {}),
                "preview_count": len(task["preview"]),
                "executed_count": len(task["executed"]),
            }

    def create_task(self, name: str, work_ids: list):
        selected = []
        idset = set(work_ids or [])
        works = scanner.get_progress(0).get("works", [])
        for w in works:
            if idset and w["id"] not in idset:
                continue
            for it in w.get("items", []):
                if it.get("type") == "video":
                    p = it.get("path", "")
                    if os.path.isfile(p) and is_path_under_root(p):
                        selected.append(os.path.abspath(p))
        return self.create_task_from_paths(name, selected)

    def create_task_from_paths(self, name: str, paths: list):
        selected = sorted(set(
            os.path.abspath(p) for p in (paths or [])
            if isinstance(p, str) and os.path.isfile(p) and is_path_under_root(p)
        ))
        tid = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        insights = {p: _empty_insight() for p in selected}
        task = {
            "id": tid,
            "name": (name or "").strip() or f"分析任务-{tid}",
            "status": "draft",
            "created_at": now,
            "source_files": selected,
            "preview": [],
            "executed": [],
            "mapping_file": "",
            "insights": insights,
            "analyze_job": {
                "state": "idle",
                "done": 0,
                "total": 0,
                "current_path": "",
                "error": "",
            },
            "eval": {
                "approved": 0,
                "reviewed": 0,
                "topk_hit": 0,
                "topk_total": 0,
                "human_pass_rate": 0.0,
                "topk_hit_rate": 0.0,
                "updated_at": "",
            },
        }
        with self._lock:
            self._tasks[tid] = task
        return task

    def get_task(self, tid: str):
        with self._lock:
            return self._tasks.get(tid)

    def start_analyze(self, tid: str):
        host, model, frames, req_timeout = _ollama_config()
        ok, oerr = _ollama_health_check(host)
        if not ok:
            return {
                "ok": False,
                "error": (
                    f"无法连接本地 Ollama（{host}）：{oerr}。"
                    "请先在本机终端运行 `ollama serve`，并确保已 `ollama pull` 视觉模型。"
                ),
            }
        resolved_model, model_source = _ollama_resolve_vision_model(host, model)
        if model_source == "not_installed":
            return {
                "ok": False,
                "error": f"未找到视觉模型「{model}」，请先执行 ollama pull qwen2.5vl:7b 或设置 MB_OLLAMA_MODEL。",
            }
        if resolved_model != model:
            logger.info("Ollama 模型自动选择: configured=%s, active=%s", model, resolved_model)
            model = resolved_model
        if "qwen" in model.lower() and "vl" in model.lower():
            # Multi-image Qwen-VL can be slow on consumer hardware.  Keep the
            # batch representative while avoiding repeated 300s timeouts.
            frames = min(frames, 4)
            req_timeout = max(req_timeout, 900)
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            if task.get("analyze_job", {}).get("state") == "running":
                return {"ok": False, "error": "该任务正在分析中，请稍候"}
            n = len(task["source_files"])
            task["status"] = "analyzing"
            task["analyze_job"] = {
                "state": "running",
                "done": 0,
                "total": n,
                "current_path": "",
                "error": "",
                "phase": "starting",
                "phase_detail": "已连接 Ollama，准备分析…",
                "ollama_host": host,
                "model": model,
            }
        threading.Thread(
            target=self._analyze_worker,
            args=(tid, host, model, frames, req_timeout),
            daemon=True,
        ).start()
        return {"ok": True, "task_id": tid, "total": n}

    def _analyze_worker(self, tid: str, ollama_host: str, model: str, frame_count: int, request_timeout: int):
        with self._lock:
            task = self._tasks.get(tid)
            paths = list(task["source_files"]) if task else []
        tmp_root = os.path.join(CACHE_DIR, "llm_analyze", tid)
        os.makedirs(tmp_root, exist_ok=True)
        for i, path in enumerate(paths):
            frames_dir = os.path.join(tmp_root, sha256_str(path)[:16])
            try:
                with self._lock:
                    t = self._tasks.get(tid)
                    if not t:
                        return
                    if t.get("analyze_job", {}).get("state") != "running":
                        return
                    t["analyze_job"]["done"] = i
                    t["analyze_job"]["current_path"] = path
                    t["analyze_job"]["phase"] = "extract_frames"
                    t["analyze_job"]["phase_detail"] = "正在抽取画面并识别音频语言…"
                    ins = t["insights"].setdefault(path, _empty_insight())
                    ins["llm_status"] = "running"
                    ins["error"] = ""
                patch_review_state_video(video_asset_id(path), {
                    "path": path,
                    "ai_analysis": {
                        "status": "running",
                        "provider": "ollama",
                        "model": model,
                        "schema_version": 4,
                        "task_id": tid,
                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                })

                def _after_frames():
                    with self._lock:
                        t2 = self._tasks.get(tid)
                        if t2 and t2.get("analyze_job", {}).get("state") == "running":
                            t2["analyze_job"]["phase"] = "ollama"
                            t2["analyze_job"]["phase_detail"] = (
                                f"正在请求 Ollama 模型「{model}」…（单条约 {request_timeout}s 超时）"
                            )

                result = _vision_analyze_video(
                    path,
                    ollama_host,
                    model,
                    frame_count,
                    frames_dir,
                    request_timeout,
                    after_frames_hook=_after_frames,
                )
                with self._lock:
                    t = self._tasks.get(tid)
                    if not t:
                        return
                    ins = t["insights"].setdefault(path, _empty_insight())
                    ins["llm_status"] = "done"
                    for k, v in result.items():
                        ins[k] = v
                    ins["confirmed_phrase"] = ""
                    ins["user_confirmed"] = False
                save_automatic_video_analysis(path, result, "ollama", model)
            except Exception as e:
                with self._lock:
                    t = self._tasks.get(tid)
                    if t:
                        ins = t["insights"].setdefault(path, _empty_insight())
                        ins["llm_status"] = "error"
                        ins["error"] = str(e)
                patch_review_state_video(video_asset_id(path), {
                    "path": path,
                    "ai_analysis": {
                        "status": "error",
                        "provider": "ollama",
                        "model": model,
                        "task_id": tid,
                        "error": str(e)[:2000],
                        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                })
            finally:
                shutil.rmtree(frames_dir, ignore_errors=True)
        with self._lock:
            t = self._tasks.get(tid)
            if t and t.get("analyze_job", {}).get("state") == "running":
                error_count = sum(
                    1 for ins in (t.get("insights") or {}).values()
                    if ins.get("llm_status") == "error"
                )
                t["analyze_job"]["state"] = "done"
                t["analyze_job"]["done"] = len(paths)
                t["analyze_job"]["current_path"] = ""
                t["analyze_job"]["phase"] = "idle"
                t["analyze_job"]["phase_detail"] = "本批视频已全部处理"
                t["status"] = "analyzed_with_errors" if error_count else "analyzed"

    def confirm_insights(self, tid: str, confirms: list):
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            insights = task.get("insights") or {}
            for it in confirms or []:
                path = it.get("path")
                if not path or path not in insights:
                    continue
                ins = insights[path]
                # 可选覆盖时间（允许用户修正）
                tstr = it.get("time")
                if isinstance(tstr, str) and tstr.strip():
                    ins["confirmed_time"] = tstr.strip()
                if not it.get("confirmed"):
                    ins["user_confirmed"] = False
                    continue
                ins["user_confirmed"] = True
                ph = it.get("phrase")
                if ph is not None:
                    ins["confirmed_phrase"] = str(ph).strip()
                else:
                    ins["confirmed_phrase"] = (ins.get("confirmed_phrase") or ins.get("phrase") or "").strip()
                raw_tags = it.get("tags")
                if isinstance(raw_tags, list):
                    clean = []
                    for x in raw_tags:
                        s = str(x).strip()
                        if not s:
                            continue
                        if re.search(r"[A-Za-z]", s):
                            continue
                        clean.append(s)
                    ins["confirmed_tags"] = clean
                elif isinstance(raw_tags, str):
                    clean = []
                    for t in re.split(r"[,，;；|/]", raw_tags):
                        s = t.strip()
                        if not s or re.search(r"[A-Za-z]", s):
                            continue
                        clean.append(s)
                    ins["confirmed_tags"] = clean
                else:
                    ins["confirmed_tags"] = list(ins.get("tags") or [])
            return {"ok": True, "id": tid}

    def update_eval(self, tid: str, approved: int, reviewed: int, topk_hit: int, topk_total: int):
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            approved = max(0, int(approved))
            reviewed = max(0, int(reviewed))
            topk_hit = max(0, int(topk_hit))
            topk_total = max(0, int(topk_total))
            if approved > reviewed:
                approved = reviewed
            if topk_hit > topk_total:
                topk_hit = topk_total
            human_pass_rate = (approved / reviewed * 100.0) if reviewed > 0 else 0.0
            topk_hit_rate = (topk_hit / topk_total * 100.0) if topk_total > 0 else 0.0
            task["eval"] = {
                "approved": approved,
                "reviewed": reviewed,
                "topk_hit": topk_hit,
                "topk_total": topk_total,
                "human_pass_rate": round(human_pass_rate, 2),
                "topk_hit_rate": round(topk_hit_rate, 2),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            return {"id": tid, "eval": task["eval"]}

    def build_preview(self, tid: str):
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            insights = task.get("insights") or {}
            review_videos = (load_review_state().get("videos") or {})
            pending = [
                p for p in task["source_files"]
                if (insights.get(p) or {}).get("llm_status") in ("done", "error")
                and not (insights.get(p) or {}).get("user_confirmed")
                and analysis_review_decision(review_videos.get(video_asset_id(p)) or {}).get("status")
                not in ("auto_accepted", "accepted", "excluded")
            ]
            if pending:
                return {
                    "ok": False,
                    "error": "仍有需要处理的异常分析结果；请先在“异常复核”中接受、重试或排除。",
                    "pending_confirm_paths": pending,
                }
            by_dir = {}
            for p in task["source_files"]:
                by_dir.setdefault(os.path.dirname(p), []).append(p)
            preview = []
            for d, files in by_dir.items():
                files = sorted(files)
                seq = 1
                used = set(os.listdir(d))
                for src in files:
                    ins = insights.get(src) or {}
                    decision = analysis_review_decision(review_videos.get(video_asset_id(src)) or {})
                    use_ai = ins.get("llm_status") == "done" and (
                        ins.get("user_confirmed") or decision.get("status") in ("auto_accepted", "accepted")
                    )
                    while True:
                        if use_ai:
                            phrase = (ins.get("confirmed_phrase") or ins.get("phrase") or "").strip()
                            tags = ins.get("confirmed_tags") or ins.get("tags") or []
                            name = _build_candidate_from_phrase(src, phrase, tags, seq)
                        else:
                            name = _build_candidate_filename(src, seq)
                        seq += 1
                        if name not in used:
                            used.add(name)
                            break
                    dst = os.path.join(d, name)
                    preview.append({
                        "src": src,
                        "dst": dst,
                        "dst_name": name,
                        "same_dir": os.path.dirname(src) == os.path.dirname(dst),
                    })
            task["preview"] = preview
            task["status"] = "previewed"
            return {
                "ok": True,
                "id": task["id"],
                "preview": preview,
                "file_count": len(task["source_files"]),
            }

    def execute(self, tid: str):
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            if not task["preview"]:
                return {"ok": False, "error": "preview not ready"}
            mapping = []
            errors = []
            for row in task["preview"]:
                src = row["src"]
                dst = row["dst"]
                if os.path.dirname(src) != os.path.dirname(dst):
                    errors.append({"src": src, "error": "cross-directory rename is forbidden"})
                    continue
                if not is_path_under_root(src) or not is_path_under_root(os.path.dirname(dst)):
                    errors.append({"src": src, "error": "forbidden"})
                    continue
                if not os.path.exists(src):
                    errors.append({"src": src, "error": "source missing"})
                    continue
                if os.path.exists(dst):
                    errors.append({"src": src, "error": "target exists"})
                    continue
                try:
                    os.rename(src, dst)
                    mapping.append({"old": src, "new": dst})
                except Exception as e:
                    errors.append({"src": src, "error": str(e)})
            map_path = os.path.join(CACHE_DIR, f"rename_map_{tid}.json")
            with open(map_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "task_id": tid,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "mapping": mapping,
                        "errors": errors,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            task["mapping_file"] = map_path
            task["executed"] = mapping
            task["status"] = "done" if not errors else "partial"
            return {"ok": True, "renamed": len(mapping), "errors": errors, "mapping_file": map_path}

    def rollback(self, tid: str):
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            reverted = 0
            errors = []
            for row in reversed(task["executed"]):
                oldp = row["old"]
                newp = row["new"]
                try:
                    if os.path.exists(newp) and not os.path.exists(oldp):
                        os.rename(newp, oldp)
                        reverted += 1
                except Exception as e:
                    errors.append({"new": newp, "error": str(e)})
            if reverted > 0 and not errors:
                task["status"] = "rolled_back"
            return {"ok": True, "reverted": reverted, "errors": errors}


analysis_tasks = AnalysisTaskManager()

_ai_resume_lock = threading.Lock()
_ai_resume_roots_active: set[str] = set()


def resume_pending_video_analysis(expected_root: str | None = None) -> None:
    """Auto AI scan is disabled in V2.5.0 to return control to the user. (Silent queue decoupled)."""
    return
    
    if not MB_ENABLE_AI:
        return
    root = os.path.realpath(expected_root or get_scan_root())
    with _ai_resume_lock:
        if root in _ai_resume_roots_active:
            return
        _ai_resume_roots_active.add(root)
    try:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if os.path.realpath(get_scan_root()) != root:
                return
            progress = scanner.get_progress(0)
            if progress.get("done"):
                break
            time.sleep(1)
        state = load_review_state(root)
        ledger_videos = state.get("videos") or {}
        paths = []
        for work in progress.get("works") or []:
            for item in work.get("items") or []:
                if item.get("type") != "video":
                    continue
                path = item.get("path")
                if not isinstance(path, str) or not os.path.isfile(path) or not is_path_under_root(path):
                    continue
                entry = ledger_videos.get(video_asset_id(path)) or {}
                ai = entry.get("ai_analysis") if isinstance(entry.get("ai_analysis"), dict) else {}
                if ai.get("status") == "done" and int(ai.get("schema_version") or 0) >= 4:
                    continue
                paths.append(path)
        if not paths:
            return
        queued_tasks = []
        for path in sorted(set(paths)):
            patch_review_state_video(video_asset_id(path), {
                "path": path,
                "ai_analysis": {
                    "status": "queued",
                    "schema_version": 4,
                    "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            })
            task = analysis_tasks.create_task_from_paths(f"自动分析 · {os.path.basename(path)}", [path])
            task["status"] = "queued"
            queued_tasks.append(task)
        logger.info("扫描后已创建 %s 个单视频 AI 任务", len(queued_tasks))
        host, _, _, _ = _ollama_config()
        ollama_deadline = time.monotonic() + 1800
        while True:
            ok, error = _ollama_health_check(host, timeout=10)
            if ok:
                break
            if time.monotonic() >= ollama_deadline or os.path.realpath(get_scan_root()) != root:
                logger.warning("发现 %s 个待续分析视频，但 Ollama 持续不可用: %s", len(paths), error)
                return
            logger.info("等待 Ollama 后恢复 %s 个视频分析: %s", len(paths), error)
            time.sleep(30)
        for index, task in enumerate(queued_tasks, 1):
            if os.path.realpath(get_scan_root()) != root:
                return
            result = analysis_tasks.start_analyze(task["id"])
            if not result or not result.get("ok"):
                task["status"] = "error"
                logger.warning("单视频 AI 任务启动失败 %s: %s", task["id"], (result or {}).get("error", "unknown"))
                continue
            logger.info("单视频 AI 分析 %s/%s: task=%s", index, len(queued_tasks), task["id"])
            while os.path.realpath(get_scan_root()) == root:
                current = analysis_tasks.get_task(task["id"])
                state_name = (current or {}).get("analyze_job", {}).get("state")
                if state_name in ("done", "error"):
                    break
                time.sleep(1)
    finally:
        with _ai_resume_lock:
            _ai_resume_roots_active.discard(root)


def _resume_analysis_after_active_batch(expected_root: str) -> None:
    """Wait for the serial worker, then pick up user-requested retries."""
    root = os.path.realpath(expected_root)
    while os.path.realpath(get_scan_root()) == root:
        with _ai_resume_lock:
            active = root in _ai_resume_roots_active
        if not active:
            resume_pending_video_analysis(root)
            return
        time.sleep(2)


def trigger_exit():
    """结束 HTTP 服务与扫描线程池（在后台线程调用 shutdown，避免死锁）。"""

    def _job():
        time.sleep(0.12)
        try:
            scanner._executor.shutdown(wait=False)
        except Exception:
            pass
        srv = _http_server
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass

    threading.Thread(target=_job, daemon=True).start()


# ===================== HTTP 处理器 =====================
class Handler(BaseHTTPRequestHandler):
    def _load_html_template(self) -> str:
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'index.html')
        with open(template_path, 'r', encoding='utf-8') as f:
            return f.read()

    def log_message(self, format, *args):
        pass

    def _is_authorized(self) -> bool:
        if not _access_token_required():
            return True
        if not ACCESS_TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], ACCESS_TOKEN):
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == "mb_access_token":
                return secrets.compare_digest(unquote(value), ACCESS_TOKEN)
        return False

    def _client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _host_header_allowed(self) -> bool:
        if _access_token_required() and ACCESS_TOKEN:
            return True
        try:
            hostname = urlparse("//" + self.headers.get("Host", "")).hostname or ""
            return hostname.lower() == "localhost" or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        if origin == "null":
            return False
        return (urlparse(origin).netloc or "").lower() == self.headers.get("Host", "").lower()

    def _require_request_security(self, *, mutating: bool = False) -> bool:
        if not self._host_header_allowed() or (mutating and not self._origin_allowed()):
            self._send_json({"ok": False, "error": "request origin rejected"}, 403)
            return False
        return self._require_authorized()

    def _require_authorized(self) -> bool:
        if self._is_authorized():
            return True
        self._send_json({"ok": False, "error": "access token required"}, 401)
        return False

    def _body_too_large(self) -> bool:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"ok": False, "error": "invalid content length"}, 400)
            return True
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "request body too large"}, 413)
            return True
        return False

    def _stream_transcoded_mp4(self, fpath: str):
        has_audio = ffprobe_has_audio(fpath)
        hw = resolve_ffmpeg_hw()
        active = hw.get("active") or "off"
        device = hw.get("device")
        using_hw = active in ("vaapi", "qsv", "nvenc", "amf")
        cmd = _ffmpeg_build_transcode_cmd(
            fpath,
            "pipe:1",
            has_audio=has_audio,
            hw_mode=active if using_hw else "off",
            vaapi_device=device,
            for_pipe=True,
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        _register_ffmpeg_proc(fpath, proc)
        try:
            first = proc.stdout.read(65536) if proc.stdout else b""
            if not first:
                err = (proc.stderr.read(800) if proc.stderr else b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                if using_hw:
                    logger.warning("实时转码 %s 失败，回退 CPU: %s", active, err or "empty stdout")
                    _unregister_ffmpeg_proc(fpath, proc)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    cmd = _ffmpeg_build_transcode_cmd(
                        fpath,
                        "pipe:1",
                        has_audio=has_audio,
                        hw_mode="off",
                        vaapi_device=None,
                        for_pipe=True,
                    )
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0,
                    )
                    _register_ffmpeg_proc(fpath, proc)
                    first = proc.stdout.read(65536) if proc.stdout else b""
                    using_hw = False
                if not first:
                    err = (proc.stderr.read(800) if proc.stderr else b"").decode(
                        "utf-8", errors="replace"
                    ).strip()
                    logger.warning("转码无输出 %s: %s", fpath, err or "empty stdout")
                    self.send_error(502, "transcode failed")
                    return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(first)
            while True:
                chunk = proc.stdout.read(131072)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ValueError):
            pass
        finally:
            _unregister_ffmpeg_proc(fpath, proc)
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass

    def _send_json(self, data, code=200, no_store=False):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if no_store:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def _send_file(self, path: str, content_type: str = None):
        if not os.path.exists(path) or not os.path.isfile(path):
            self.send_error(404)
            return
        if content_type is None:
            content_type, _ = mimetypes.guess_type(path)
            if not content_type:
                content_type = "application/octet-stream"
        size = os.path.getsize(path)

        range_hdr = self.headers.get("Range", "")
        start, end = 0, size - 1
        status = 200
        if range_hdr.startswith("bytes="):
            try:
                rng = range_hdr[6:].strip().split("-")
                if rng[0]:
                    start = int(rng[0])
                if len(rng) > 1 and rng[1]:
                    end = int(rng[1])
                else:
                    end = size - 1
                if start > end or start >= size:
                    raise ValueError("invalid range")
                status = 206
            except Exception:
                start, end = 0, size - 1
                status = 200

        self.send_response(status)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        try:
            with open(path, "rb") as f:
                f.seek(start)
                remain = end - start + 1
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except BrokenPipeError:
            pass

    def do_OPTIONS(self):
        self.send_error(403, "cross-origin requests disabled")

    def _read_json_body(self) -> tuple[dict | None, str | None]:
        if self._body_too_large():
            return None, "response already sent"
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "invalid json"
        if not isinstance(data, dict):
            return None, "json body must be object"
        return data, None

    def do_PATCH(self):
        if not self._require_request_security(mutating=True) or self._body_too_large():
            return
        parsed = urlparse(self.path)
        work_match = re.fullmatch(r"/api/review-state/work/([0-9a-fA-F]{16,64})", parsed.path or "")
        video_match = re.fullmatch(r"/api/review-state/video/([0-9a-fA-F]{16,64})", parsed.path or "")
        if not work_match and not video_match:
            self.send_error(404)
            return
        data, err = self._read_json_body()
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if video_match:
            result = patch_review_state_video(video_match.group(1), data or {})
        else:
            result = patch_review_state_work(work_match.group(1), data or {})
        self._send_json(result, 200 if result.get("ok") else 400)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if not self._host_header_allowed():
            self._send_json({"ok": False, "error": "request origin rejected"}, 403)
            return
        if path == "/" and ACCESS_TOKEN and qs.get("token"):
            supplied = qs.get("token", [""])[0]
            if secrets.compare_digest(supplied, ACCESS_TOKEN):
                self.send_response(303)
                self.send_header(
                    "Set-Cookie",
                    f"mb_access_token={quote(ACCESS_TOKEN, safe='')}; HttpOnly; SameSite=Strict; Path=/",
                )
                self.send_header("Location", "/")
                self.end_headers()
                return
        if path == "/health" and (
            not _access_token_required() or self._client_is_loopback() or self._is_authorized()
        ):
            payload, code = build_health_payload()
            self._send_json(payload, code=code, no_store=True)
            return
        if not self._require_authorized():
            return
        if path == "/":
            presets_json = json.dumps(get_scan_presets(), ensure_ascii=False)
            page = (
                self._load_html_template().replace("__MB_ROOT_DIR__", html_mod.escape(get_scan_root()))
                .replace("__MB_CACHE_DIR__", html_mod.escape(CACHE_DIR))
                .replace("__MB_CACHE_DIR_JSON__", json.dumps(CACHE_DIR, ensure_ascii=False))
                .replace("__THUMB_COUNT__", str(THUMB_COUNT))
                .replace("__APP_VERSION__", html_mod.escape(APP_VERSION))
                .replace(
                    "__MB_SCAN_READONLY__",
                    "true" if scan_root_readonly() else "false",
                )
                .replace(
                    "__MB_SCAN_PRESET_MODE__",
                    "true" if scan_presets_enabled() else "false",
                )
                .replace("__MB_SCAN_PRESETS_JSON__", presets_json)
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        elif path == "/api/progress":
            since = int(qs.get("since", ["0"])[0])
            prog = scanner.get_progress(since)
            self._send_json(prog)
        elif path == "/api/tasks":
            self._send_json({"ok": True, "tasks": analysis_tasks.list_tasks()}, no_store=True)

        elif path == "/api/analysis-review":
            self._send_json(analysis_review_payload(), no_store=True)

        elif path == "/api/settings":
            self._send_json(cache_settings_payload(), no_store=True)

        elif path == "/api/works":
            with scanner.lock:
                body = {
                    "ok": True,
                    "works": list(scanner.works),
                    "done": scanner.done,
                    "scanned": scanner.scanned_dirs,
                    "total": scanner.total_dirs,
                    "enum_error": scanner.enum_error,
                    "scan_root": get_scan_root(),
                }
            self._send_json(body, no_store=True)

        elif path == "/api/tags":
            oh, om, _, _ = _ollama_config()
            tags: list[str] = []
            reachable = False
            err = None
            try:
                o_url = f"{oh.rstrip('/')}/api/tags"
                o_req = Request(o_url, method="GET")
                with urlopen(o_req, timeout=3.5) as o_resp:
                    raw = json.loads(o_resp.read().decode("utf-8"))
                reachable = True
                for m in raw.get("models") or []:
                    if isinstance(m, dict):
                        name = m.get("name")
                        if name:
                            tags.append(str(name))
            except Exception as e:
                err = str(e)[:400]
            self._send_json(
                {"ok": reachable, "tags": tags, "error": err, "host": oh, "model_default": om},
                no_store=True,
            )

        elif path == "/api/delete-trash":
            items = delete_trash_list()
            self._send_json({"ok": True, "items": items, "count": len(items)}, no_store=True)

        elif path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/") :]
            if not rest or "/" in rest:
                self.send_error(404)
                return
            det = analysis_tasks.get_task_detail(rest)
            if not det:
                self._send_json({"ok": False, "error": "task not found"}, 404, no_store=True)
                return
            self._send_json({"ok": True, "task": det}, no_store=True)

        elif path == "/api/preview-thumb":
            fpath = qs.get("path", [""])[0]
            fpath = unquote(fpath)
            if not fpath or not os.path.isfile(fpath):
                self.send_error(404)
                return
            try:
                rp = os.path.realpath(os.path.abspath(fpath))
            except OSError:
                self.send_error(404)
                return
            if not is_path_under_root(rp):
                self.send_error(403)
                return
            if os.path.splitext(rp)[1].lower() not in VIDEO_EXTS:
                self.send_error(400)
                return
            try:
                tp = generate_video_thumb_single(rp)
            except Exception:
                self.send_error(500)
                return
            if not tp or not os.path.isfile(tp):
                self.send_error(404)
                return
            self._send_file(tp, "image/jpeg")

        elif path.startswith("/thumb/"):
            parts = path.split("/")
            if (
                len(parts) == 4
                and re.fullmatch(r"[0-9a-fA-F]{16}", parts[2] or "")
                and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[3] or "")
                and parts[3] not in (".", "..")
            ):
                file_hash = parts[2]
                idx_name = parts[3]
                thumb_path = os.path.realpath(os.path.join(_thumb_cache_dir(file_hash), idx_name))
                cache_root = os.path.realpath(CACHE_DIR)
                if thumb_path.startswith(cache_root + os.sep):
                    self._send_file(thumb_path, "image/jpeg")
                else:
                    self.send_error(403)
            else:
                self.send_error(404)

        elif path in ("/api/preview-info", "/api/preview-segment"):
            import on_demand
            try:
                source = qs.get("path", [""])[0]
                if path == "/api/preview-info":
                    self._send_json(on_demand.info(sys.modules[__name__], source), no_store=True)
                else:
                    target = on_demand.segment(sys.modules[__name__], source, int(qs.get("index", ["-1"])[0]))
                    self._send_file(target, "video/mp4")
            except (ValueError, OSError) as exc:
                self._send_json({"error": str(exc)}, 400, no_store=True)
            except Exception as exc:
                logger.warning("On-demand preview failed: %s", exc)
                self._send_json({"error": "此片段无法预览，请尝试兼容播放"}, 502, no_store=True)

        elif path == "/api/cache-summary":
            import cache_manager
            self._send_json(cache_manager.snapshot(sys.modules[__name__]), no_store=True)

        elif path == "/api/play-ready":
            fpath = qs.get("path", [""])[0]
            fpath = unquote(fpath)
            force_transcode = (qs.get("force", [""])[0] or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            self._send_json(play_ready_payload(fpath, force_transcode=force_transcode), no_store=True)

        elif path == "/api/review-state":
            self._send_json(review_state_get_payload(), no_store=True)

        elif path == "/api/preferences/summary":
            self._send_json(preference_summary_payload(), no_store=True)

        elif path == "/file":
            fpath = qs.get("path", [""])[0]
            fpath = unquote(fpath)
            if (
                os.path.exists(fpath)
                and os.path.isfile(fpath)
                and is_servable_file_path(fpath)
            ):
                self._send_file(fpath)
            else:
                self.send_error(404)

        elif path == "/play":
            fpath = qs.get("path", [""])[0]
            fpath = unquote(fpath)
            if not (os.path.isfile(fpath) and is_path_under_root(fpath)):
                self.send_error(404)
                return
            ext = os.path.splitext(fpath)[1].lower()
            if ext not in VIDEO_EXTS:
                self.send_error(400)
                return
            fa, fe = _tool_version_ok(FFMPEG_BIN)
            if not fa:
                self.send_error(503, f"ffmpeg unavailable: {fe or 'missing'}")
                return
            cached = play_cache_path(fpath)
            if os.path.isfile(cached) and os.path.getsize(cached) > 512:
                self._send_file(cached, "video/mp4")
                return
            self._stream_transcoded_mp4(fpath)

        elif path == "/open":
            fpath = qs.get("path", [""])[0]
            fpath = unquote(fpath)
            if os.path.exists(fpath) and is_path_under_root(fpath):
                try:
                    target = fpath if os.path.isdir(fpath) else os.path.dirname(fpath)
                    if not is_path_under_root(target):
                        self._send_json({"ok": False, "error": "forbidden"}, 403)
                    else:
                        ok = open_in_file_manager(target)
                        if ok:
                            self._send_json({"ok": True})
                        else:
                            self._send_json({"ok": False, "error": "open failed"}, 500)
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, 500)
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)

        else:
            self.send_error(404)

    def do_POST(self):
        if not self._require_request_security(mutating=True) or self._body_too_large():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/shutdown":
            self._send_json({"ok": True})
            trigger_exit()
            return
        if parsed.path == "/api/tasks":
            if not MB_ENABLE_AI:
                self._send_json({"ok": False, "error": "AI 分析功能已通过 MB_ENABLE_AI=0 禁用"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            task = analysis_tasks.create_task(data.get("name", ""), data.get("work_ids", []))
            self._send_json({"ok": True, "task": {"id": task["id"], "name": task["name"], "file_count": len(task["source_files"])}})
            return
        if parsed.path == "/api/analysis-review/action":
            data, err = self._read_json_body()
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            ret = apply_analysis_review_action(data.get("video_ids") or [], str(data.get("action") or ""))
            self._send_json(ret, 200 if ret.get("ok") else 400)
            return
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/analyze", parsed.path or "")
        if m:
            if not MB_ENABLE_AI:
                self._send_json({"ok": False, "error": "AI 分析功能已禁用"}, 403)
                return
            ret = analysis_tasks.start_analyze(m.group(1))
            if ret is None:
                self._send_json({"ok": False, "error": "task not found"}, 404)
                return
            if not ret.get("ok"):
                self._send_json(ret, 400)
                return
            self._send_json(ret)
            return
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/confirm", parsed.path or "")
        if m:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            ret = analysis_tasks.confirm_insights(m.group(1), data.get("confirms") or [])
            if not ret:
                self._send_json({"ok": False, "error": "task not found"}, 404)
                return
            self._send_json(ret)
            return
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/preview", parsed.path or "")
        if m:
            ret = analysis_tasks.build_preview(m.group(1))
            if not ret:
                self._send_json({"ok": False, "error": "task not found"}, 404)
                return
            if not ret.get("ok", True):
                self._send_json(ret, 400)
                return
            self._send_json({"ok": True, **ret})
            return
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/execute", parsed.path or "")
        if m:
            ret = analysis_tasks.execute(m.group(1))
            if not ret:
                self._send_json({"ok": False, "error": "task not found"}, 404)
                return
            if not ret.get("ok"):
                self._send_json(ret, 400)
                return
            self._send_json(ret)
            return
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/rollback", parsed.path or "")
        if m:
            ret = analysis_tasks.rollback(m.group(1))
            if not ret:
                self._send_json({"ok": False, "error": "task not found"}, 404)
                return
            self._send_json(ret)
            return
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/evaluation", parsed.path or "")
        if m:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            ret = analysis_tasks.update_eval(
                m.group(1),
                data.get("approved", 0),
                data.get("reviewed", 0),
                data.get("topk_hit", 0),
                data.get("topk_total", 0),
            )
            if not ret:
                self._send_json({"ok": False, "error": "task not found"}, 404)
                return
            self._send_json({"ok": True, "evaluation": ret})
            return
        if parsed.path == "/api/set-scan-root":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            p = data.get("path", "")
            if not isinstance(p, str) or not p.strip():
                self._send_json({"ok": False, "error": "missing path"}, 400)
                return
            preset_err = preset_reject_reason(p)
            if preset_err:
                code = 403 if scan_presets_enabled() else 400
                self._send_json({"ok": False, "error": preset_err}, code)
                return
            if replace_scan_root(p, persist=True):
                self._send_json({"ok": True, "path": get_scan_root()})
            else:
                _resolved, err = resolve_scan_root_path(p)
                self._send_json(
                    {"ok": False, "error": err or "路径不存在或不是文件夹"},
                    400,
                )
            return
        if parsed.path == "/api/cache-clear":
            import cache_manager
            data, err = self._read_json_body()
            if err:
                self._send_json({"error": err}, 400)
                return
            try:
                kind = data.get("kind")
                if kind == "orphans":
                    self._send_json(cache_manager.clear_orphans(sys.modules[__name__]), no_store=True)
                else:
                    self._send_json(cache_manager.clear(sys.modules[__name__], kind, data.get("token")), no_store=True)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400, no_store=True)
            return
        if parsed.path == "/api/set-cache-dir":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            p = data.get("path", "")
            if not isinstance(p, str) or not p.strip():
                self._send_json({"ok": False, "error": "missing path"}, 400)
                return
            ok, payload = replace_cache_dir(p)
            self._send_json(payload, 200 if ok else 400)
            return
        if parsed.path == "/api/delete-trash/remove":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            paths = data.get("paths")
            if not isinstance(paths, list):
                self._send_json({"ok": False, "error": "paths must be array"}, 400)
                return
            n = delete_trash_remove_paths(paths)
            self._send_json({"ok": True, "count": n})
            return
        if parsed.path == "/api/delete-trash/delete-selected":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            paths = data.get("paths")
            if not isinstance(paths, list):
                self._send_json({"ok": False, "error": "paths must be array"}, 400)
                return
            self._send_json(delete_trash_delete_selected(paths))
            return
        if parsed.path == "/api/delete-trash/clear":
            delete_trash_clear()
            self._send_json({"ok": True, "count": 0})
            return
        if parsed.path == "/api/delete-trash/retry-all":
            self._send_json(delete_trash_retry_all())
            return
        if parsed.path == "/api/works/delete-all":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            work_path = data.get("work_path", "")
            paths = data.get("paths")
            if not isinstance(paths, list):
                self._send_json({"ok": False, "error": "paths must be array"}, 400)
                return
            result = delete_work_all_media_and_folder(work_path, paths)
            if not result.get("ok"):
                self._send_json(result, 400)
                return
            self._send_json(result)
            return
        if parsed.path == "/api/review-state/clear":
            clear_review_state()
            self._send_json({"ok": True, "state": empty_review_state()})
            return
        if parsed.path == "/api/review-state/import":
            data, err = self._read_json_body()
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            tags = data.get("tags") if data else {}
            if not isinstance(tags, dict):
                self._send_json({"ok": False, "error": "tags must be object"}, 400)
                return
            self._send_json(import_review_tags(tags))
            return
        if parsed.path != "/delete":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json({"ok": False, "error": "invalid json"}, 400)
            return
        fpath = data.get("path", "")
        work_path = data.get("work_path", "")
        if not isinstance(fpath, str) or not fpath:
            self._send_json({"ok": False, "error": "missing path"}, 400)
            return
        if not os.path.exists(fpath) or not os.path.isfile(fpath):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        if not is_path_under_root(fpath):
            self._send_json({"ok": False, "error": "forbidden"}, 403)
            return
        cleanup_work_path = ""
        if isinstance(work_path, str) and work_path.strip():
            try:
                candidate = os.path.realpath(
                    os.path.abspath(os.path.expanduser(work_path.strip()))
                )
                if (
                    _can_remove_work_folder_dir(candidate)
                    and _path_under_work_dir(fpath, candidate)
                ):
                    cleanup_work_path = candidate
            except OSError:
                pass
        try:
            _safe_remove(fpath)
            remove_media_thumb_cache(fpath)
            folder_removed = False
            if cleanup_work_path:
                folder_removed = try_remove_empty_work_folder(cleanup_work_path)
            self._send_json({"ok": True, "folder_removed": folder_removed})
        except Exception as e:
            err = str(e)
            trash_n = 0
            queued = False
            try:
                if os.path.isfile(fpath) and is_path_under_root(fpath):
                    trash_n = delete_trash_add(fpath, err)
                    queued = True
            except Exception:
                pass
            self._send_json(
                {"ok": False, "error": err, "queued": queued, "trash_count": trash_n},
                500,
            )


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    pass




def main():
    global _http_server
    if _host_requires_access_token() and not ACCESS_TOKEN:
        raise SystemExit(
            "MB_ACCESS_TOKEN is required when MB_HOST is not localhost/127.0.0.1/::1"
        )
    _apply_perf_profile_for_scan_root(get_scan_root())
    auto_open = os.environ.get(
        "MB_AUTO_OPEN",
        "1" if getattr(sys, "frozen", False) else "0",
    ).strip().lower() in ("1", "yes", "true", "on")
    logger.info("Media Browser v%s", APP_VERSION)
    logger.info("扫描目录: %s", get_scan_root())
    logger.info("缩略图缓存: %s", CACHE_DIR)
    logger.info(
        "扫描并发=%s，每视频条带缩略图=%s（可用 MB_SCAN_WORKERS / MB_THUMB_COUNT / MB_DISK_PROFILE 调整）",
        MAX_WORKERS,
        THUMB_COUNT,
    )
    hw = resolve_ffmpeg_hw()
    logger.info(
        "FFmpeg 硬件加速: configured=%s, active=%s, available=%s",
        hw.get("configured"),
        hw.get("active"),
        hw.get("available"),
    )
    if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical"):
        logger.info(
            "MB_DISK_PROFILE=%s：已限制并发与缩略图帧数以减轻硬盘负载",
            DISK_PROFILE,
        )
    logger.info("监听 %s:%s（本机访问 http://localhost:%s）", HOST, PORT, PORT)
    if _access_token_required():
        logger.info("访问令牌已启用；首次访问使用 /?token=<MB_ACCESS_TOKEN>")
    logger.info("浏览器页眉可点「退出应用」停止服务")
    _oh, _om, _of, _ot = _ollama_config()
    logger.info(
        "AI 视频分析：本地 Ollama %s · 模型 %s（抽帧数 MB_ANALYZE_FRAME_COUNT=%s，超时 MB_OLLAMA_TIMEOUT=%ss；请 ollama pull 视觉模型）",
        _oh,
        _om,
        _of,
        _ot,
    )
    if auto_open:
        logger.info("将自动打开浏览器（如需关闭请设 MB_AUTO_OPEN=0）")

        def _open_browser():
            import webbrowser

            time.sleep(1.0)
            suffix = f"?token={quote(ACCESS_TOKEN, safe='')}" if _access_token_required() else ""
            webbrowser.open(f"http://127.0.0.1:{PORT}/{suffix}")

        threading.Thread(target=_open_browser, daemon=True).start()

    logger.info("按 Ctrl+C 停止")
    if should_auto_scan_on_startup():
        scanner.start()
        threading.Thread(
            target=resume_pending_video_analysis,
            args=(get_scan_root(),),
            daemon=True,
        ).start()
    else:
        scanner.mark_idle()
        logger.info(
            "已配置 MB_SCAN_PRESETS，启动时不自动扫描；请在页内选择媒体库后点「切换并扫描」（MB_AUTO_SCAN=1 可恢复启动即扫）"
        )
    server = ThreadedHTTPServer((HOST, PORT), Handler)
    _http_server = server

    def _sigterm_handler(signum, frame):
        logger.info("收到终止信号，退出中…")
        trigger_exit()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _sigterm_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("退出中…")
    finally:
        try:
            scanner._executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
