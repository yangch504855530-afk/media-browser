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
  MB_CACHE_DIR 缩略图缓存目录
  MB_PORT      端口（默认 8765）
  MB_HOST      监听地址（默认 0.0.0.0；仅本机可设 127.0.0.1）
  MB_AUTO_OPEN 是否启动后自动打开浏览器（打包默认为是；脚本默认为否，设为 1 可开启）
  MB_SCAN_WORKERS   同时处理「作品」任务的线程数（默认 2；机械盘/NAS 建议 1～2）
  MB_THUMB_COUNT    每个视频条带缩略图帧数（默认 5；越大越慢、越伤盘）
  MB_DISK_PROFILE   设为 slow / nas / hdd / mechanical 时自动收紧并发与缩略图，减轻随机读
  MB_OLLAMA_HOST  本地 Ollama 地址，默认 http://127.0.0.1:11434（数据不出本机）
  MB_OLLAMA_MODEL 视觉模型名，默认 llava（须 ollama pull 过；也可用 moondream、llava-phi3 等）
  MB_OLLAMA_TIMEOUT  单次请求超时秒数，默认 300
  MB_ANALYZE_FRAME_COUNT  每个视频抽帧送模型，默认 5，范围 2～12
  MB_LOG_LEVEL   日志级别：DEBUG / INFO / WARNING / ERROR（默认 INFO）
  MB_LOG_FORMAT  日志格式：text（默认，TTY 下彩色）或 json（单行 JSON，便于采集）

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
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 配置 =====================
APP_VERSION = "1.2.3"


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
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = os.path.join(meipass, name)
            if os.path.isfile(bundled):
                return bundled
    import shutil as _sh

    w = _sh.which(name)
    return w if w else name


if getattr(sys, "frozen", False):
    _dr = os.path.expanduser("~/Documents/MediaBrowser")
    _dc = os.path.expanduser("~/Library/Application Support/Media Browser/thumbs")
    ROOT_DIR = os.environ.get("MB_ROOT_DIR") or _dr
    CACHE_DIR = os.environ.get("MB_CACHE_DIR") or _dc
else:
    if sys.platform == "darwin":
        _default_root = "/Volumes/Untitled/pri"
    else:
        # Windows / Linux 默认使用用户目录下的 MediaBrowser 文件夹，避免指向不存在的卷
        _default_root = os.path.expanduser("~/MediaBrowser")
    ROOT_DIR = os.environ.get("MB_ROOT_DIR", _default_root)
    CACHE_DIR = os.environ.get(
        "MB_CACHE_DIR", os.path.expanduser("~/.cache/media-browser/thumbs")
    )

HOST = os.environ.get("MB_HOST", "0.0.0.0")
PORT = int(os.environ.get("MB_PORT", "8765"))
THUMB_WIDTH = 400

# 并发默认 2（原 4）：多任务并行会对 NAS/机械盘产生大量随机寻道；SSD 可用 MB_SCAN_WORKERS=4
DISK_PROFILE = os.environ.get("MB_DISK_PROFILE", "").strip().lower()
_DISK_PROFILE_ENV_SET = bool(DISK_PROFILE)
_SCAN_WORKERS_ENV_SET = os.environ.get("MB_SCAN_WORKERS") not in (None, "")
_THUMB_COUNT_ENV_SET = os.environ.get("MB_THUMB_COUNT") not in (None, "")
_MAX_DEFAULT = 2
_THUMB_DEFAULT = 5
if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical"):
    _MAX_DEFAULT = 1
    _THUMB_DEFAULT = 2

MAX_WORKERS = _int_env("MB_SCAN_WORKERS", _MAX_DEFAULT, 1, 16)
THUMB_COUNT = _int_env("MB_THUMB_COUNT", _THUMB_DEFAULT, 1, 30)
if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical"):
    MAX_WORKERS = min(MAX_WORKERS, 2)
    THUMB_COUNT = min(THUMB_COUNT, 3)


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
        THUMB_COUNT = 2 if profile in ("slow", "nas", "hdd", "mechanical") else 4
    if profile in ("slow", "nas", "hdd", "mechanical"):
        MAX_WORKERS = min(MAX_WORKERS, 2)
        THUMB_COUNT = min(THUMB_COUNT, 3)
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


# 通用占位图
PLACEHOLDER = os.path.join(CACHE_DIR, "_placeholder.jpg")
if not os.path.exists(PLACEHOLDER):
    subprocess.run([
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=#1a1a1a:s=400x300",
        "-frames:v", "1", "-q:v", "3", PLACEHOLDER
    ], capture_output=True)

# ===================== 工具函数 =====================
def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


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
        thumb_dir = os.path.join(CACHE_DIR, key)
        if os.path.isdir(thumb_dir):
            shutil.rmtree(thumb_dir, ignore_errors=True)
    except Exception:
        pass


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
                os.remove(p)
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
                    os.remove(canon)
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
            os.remove(fp)
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
    file_hash = sha256_str(os.path.abspath(video_path))
    thumb_dir = os.path.join(CACHE_DIR, file_hash)
    os.makedirs(thumb_dir, exist_ok=True)
    dst = os.path.join(thumb_dir, "0.jpg")
    if os.path.exists(dst) and os.path.getsize(dst) > 100:
        return dst
    info = video_info if video_info is not None else get_video_info(video_path)
    ss = "00:00:01"
    if info["duration"] > 3:
        ss = str(info["duration"] / 3)
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-an", "-dn", "-sn",
        "-ss", ss, "-i", video_path,
        "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-1", "-q:v", "3", dst
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        pass
    ensure_placeholder(dst)
    return dst


def generate_video_thumbs(video_path: str, count: int = None, video_info: dict = None) -> list:
    if count is None:
        count = THUMB_COUNT
    file_hash = sha256_str(os.path.abspath(video_path))
    thumb_dir = os.path.join(CACHE_DIR, file_hash)
    os.makedirs(thumb_dir, exist_ok=True)

    expected = [os.path.join(thumb_dir, f"{i}.jpg") for i in range(count)]
    if all(os.path.exists(p) and os.path.getsize(p) > 100 for p in expected):
        return expected

    info = video_info if video_info is not None else get_video_info(video_path)
    duration = info["duration"]

    for i in range(count):
        dst = expected[i]
        if os.path.exists(dst) and os.path.getsize(dst) > 100:
            continue

        if duration and duration > 1:
            ss = duration * (i + 1) / (count + 1)
        else:
            ss = 1

        cmd = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-threads", "1", "-an", "-dn", "-sn",
            "-ss", str(ss), "-i", video_path,
            "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-1",
            "-c:v", "mjpeg", "-strict", "unofficial",
            "-q:v", "3", dst
        ]
        try:
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
    file_hash = sha256_str(os.path.abspath(image_path))
    thumb_dir = os.path.join(CACHE_DIR, file_hash)
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

    def start(self):
        self._enumerate()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _enumerate_deep_fallback(self, candidates: list, root_flat: list) -> None:
        """一级目录未发现媒体时沿整棵树查找（含符号链接目录），按顶层子文件夹分组。"""
        scan_root = os.path.realpath(get_scan_root())
        tops_found = set()
        try:
            for wroot, dirs, files in os.walk(scan_root, followlinks=True):
                dirs[:] = [d for d in dirs if d not in _SKIP_SCAN_SUBDIR_NAMES]
                for f in files:
                    if f.startswith("._"):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
                        continue
                    fp = os.path.join(wroot, f)
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
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in VIDEO_EXTS or ext in IMAGE_EXTS:
                        root_flat.append(entry.path)
                    continue
                if not entry.is_dir():
                    continue
                if entry.name in _SKIP_SCAN_SUBDIR_NAMES:
                    continue
                has_media = False
                for root, dirs, files in os.walk(entry.path, followlinks=True):
                    dirs[:] = [d for d in dirs if d not in _SKIP_SCAN_SUBDIR_NAMES]
                    for f in files:
                        ext = os.path.splitext(f)[1].lower()
                        if ext in VIDEO_EXTS or ext in IMAGE_EXTS:
                            has_media = True
                            break
                    if has_media:
                        break
                if has_media:
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
            except Exception as e:
                logger.error("扫描错误 %s: %s", label, e)
                self.scanned_dirs += 1

        self.done = True
        logger.info("扫描完成")

    def _build_work_from_items(self, items, work_path: str, display_name_override: str = None):
        if not items:
            return None
        items.sort(key=lambda x: (0 if x["type"] == "video" else 1, x["name"]))
        video_items = []
        image_items = []
        mtime = 0
        for it in items:
            try:
                mtime = max(mtime, os.path.getmtime(it["path"]))
            except Exception:
                pass
            if it["type"] == "video":
                info = get_video_info(it["path"])
                it["duration"] = info["duration"]
                it["width"] = info["width"]
                it["height"] = info["height"]
                it["codec"] = info.get("codec", "")
                it["bitrate"] = info.get("bitrate", 0)
                it["fps"] = info.get("fps", 0.0)
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

        main_video = max(video_items, key=lambda x: x["size"]) if video_items else None
        main_duration = main_video.get("duration", 0) if main_video else 0
        id_seed = os.path.abspath(work_path) + ("\n#root_flat" if display_name_override else "")
        work_id = sha256_str(id_seed)
        total_size = sum(it["size"] for it in items)
        total_duration = sum(it.get("duration", 0) for it in video_items)
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
        }

    def _process_root_flat(self):
        try:
            paths = sorted(self._pending_root_files)
            if not paths:
                return None
            items = []
            for fpath in paths:
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
            items = []
            for root, dirs, files in os.walk(work_path, followlinks=True):
                dirs[:] = [d for d in dirs if d not in _SKIP_SCAN_SUBDIR_NAMES]
                for f in files:
                    if f.startswith("._"):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    fpath = os.path.join(root, f)
                    try:
                        fsize = os.path.getsize(fpath)
                    except OSError:
                        continue
                    if ext in VIDEO_EXTS:
                        items.append({
                            "type": "video",
                            "path": fpath,
                            "name": f,
                            "size": fsize,
                        })
                    elif ext in IMAGE_EXTS:
                        items.append({
                            "type": "image",
                            "path": fpath,
                            "name": f,
                            "size": fsize,
                        })
            return self._build_work_from_items(items, work_path, None)
        except Exception as e:
            logger.warning("process error %s: %s", work_path, e)
            return None

    def get_progress(self, since: int = 0):
        with self.lock:
            return {
                "scanned": self.scanned_dirs,
                "total": self.total_dirs,
                "done": self.done,
                "works": self.works[since:],
                "next_since": len(self.works),
                "enum_error": self.enum_error,
                "scan_root": get_scan_root(),
            }


def _normalize_scan_path_input(s: str) -> str:
    """去掉首尾空白，并把弯引号等换成 ASCII，避免粘贴路径不可见字符导致目录判断失败。"""
    t = (s or "").strip()
    for a, b in (
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\ufeff", ""),
    ):
        t = t.replace(a, b)
    return t


def replace_scan_root(new_root: str) -> bool:
    """切换扫描根目录并启动新扫描任务。返回是否成功。"""
    global _scan_root, scanner
    p = os.path.realpath(os.path.abspath(os.path.expanduser(_normalize_scan_path_input(new_root))))
    try:
        if not os.path.isdir(p):
            return False
    except OSError:
        return False
    _scan_root = p
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
    try:
        old._executor.shutdown(wait=False)
    except Exception:
        pass
    return True


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


def _cache_dir_stats(cache_dir: str, max_files: int = 500_000) -> dict:
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
    }

    fa, fe = _tool_version_ok(FFMPEG_BIN)
    pa, pe = _tool_version_ok(FFPROBE_BIN)
    ffmpeg = {"available": fa, "binary": FFMPEG_BIN, "error": fe}
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
    for i in range(count):
        t = dur * (i + 1) / (count + 1)
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


def _normalize_llm_insight(raw: dict) -> dict:
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
    return {
        "time_guess": str(raw.get("time") or raw.get("时间") or "").strip(),
        "place_guess": str(raw.get("place") or raw.get("地点") or "").strip(),
        "event_guess": str(raw.get("event") or raw.get("事件") or "").strip(),
        "tags": clean_tags,
        "phrase": phrase,
    }


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
        "tags：3～8 条，每条为 2～8 个汉字为主的短关键词，语义具体；\n"
        "phrase：用中文把上述要点连成一句极短描述（约 8～24 字），适合作文件名主题，不要空格与\\/:*?\"<>|。\n"
        "只输出一个 JSON 对象，键名必须为 time, place, event, tags, phrase。不要输出其它文字或 Markdown。"
    )
    user_block = (
        f"文件名：{bn}\n"
        f"视频时长：{dur}\n"
        f"拍摄时间（元数据/文件时间推断）：{meta_time or '(空)'}\n"
        f"共 {len(b64_list)} 张代表帧。请分析并返回 JSON。"
    )
    # Ollama：同一 user 消息里附带 images 数组（每项为原始 base64，无 data: 前缀）
    url = f"{ollama_host}/api/chat"
    payload = {
        "model": model,
        "stream": False,
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
    norm = _normalize_llm_insight(raw)
    # 若模型没给出可用日期，则回填本地元数据时间（到秒）
    if not (norm.get("time_guess") or "").strip():
        if meta_time:
            norm["time_guess"] = meta_time
    return {
        "time_guess": norm["time_guess"],
        "place_guess": norm["place_guess"],
        "event_guess": norm["event_guess"],
        "tags": norm["tags"],
        "phrase": norm["phrase"],
    }


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
        selected = sorted(set(selected))
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
        with self._lock:
            task = self._tasks.get(tid)
            if not task:
                return None
            if task.get("analyze_job", {}).get("state") == "running":
                return {"ok": False, "error": "该任务正在分析中，请稍候"}
            n = len(task["source_files"])
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
                    t["analyze_job"]["phase_detail"] = "正在从视频抽取帧（ffmpeg）…"
                    ins = t["insights"].setdefault(path, _empty_insight())
                    ins["llm_status"] = "running"
                    ins["error"] = ""

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
            except Exception as e:
                with self._lock:
                    t = self._tasks.get(tid)
                    if t:
                        ins = t["insights"].setdefault(path, _empty_insight())
                        ins["llm_status"] = "error"
                        ins["error"] = str(e)
            finally:
                shutil.rmtree(frames_dir, ignore_errors=True)
        with self._lock:
            t = self._tasks.get(tid)
            if t and t.get("analyze_job", {}).get("state") == "running":
                t["analyze_job"]["state"] = "done"
                t["analyze_job"]["done"] = len(paths)
                t["analyze_job"]["current_path"] = ""
                t["analyze_job"]["phase"] = "idle"
                t["analyze_job"]["phase_detail"] = "本批视频已全部处理"

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
            pending = [
                p for p in task["source_files"]
                if (insights.get(p) or {}).get("llm_status") == "done"
                and not (insights.get(p) or {}).get("user_confirmed")
            ]
            if pending:
                return {
                    "ok": False,
                    "error": "已进行过 AI 分析：请先确认每条视频的标签/短语，再生成预览。",
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
                    use_ai = ins.get("llm_status") == "done" and ins.get("user_confirmed")
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
    def log_message(self, format, *args):
        pass

    def _stream_transcoded_mp4(self, fpath: str):
        has_audio = ffprobe_has_audio(fpath)
        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
            "-nostdin", "-threads", "2",
            "-i", fpath,
            "-map", "0:v:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
        ]
        if has_audio:
            cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
        else:
            cmd += ["-an"]
        cmd += [
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            while True:
                chunk = proc.stdout.read(131072)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    def _send_json(self, data, code=200, no_store=False):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            page = (
                HTML_PAGE.replace("__MB_ROOT_DIR__", html_mod.escape(get_scan_root()))
                .replace("__THUMB_COUNT__", str(THUMB_COUNT))
                .replace("__APP_VERSION__", html_mod.escape(APP_VERSION))
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        elif path == "/health":
            payload, code = build_health_payload()
            self._send_json(payload, code=code, no_store=True)

        elif path == "/api/progress":
            since = int(qs.get("since", ["0"])[0])
            prog = scanner.get_progress(since)
            self._send_json(prog)
        elif path == "/api/tasks":
            self._send_json({"ok": True, "tasks": analysis_tasks.list_tasks()}, no_store=True)

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
            if len(parts) >= 4:
                file_hash = parts[2]
                idx_name = parts[3]
                thumb_path = os.path.join(CACHE_DIR, file_hash, idx_name)
                self._send_file(thumb_path, "image/jpeg")
            else:
                self.send_error(404)

        elif path == "/file":
            fpath = qs.get("path", [""])[0]
            fpath = unquote(fpath)
            if (
                os.path.exists(fpath)
                and os.path.isfile(fpath)
                and is_path_under_root(fpath)
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
            if not video_needs_transcoded_play(fpath):
                self.send_error(400)
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
        parsed = urlparse(self.path)
        if parsed.path == "/api/shutdown":
            self._send_json({"ok": True})
            trigger_exit()
            return
        if parsed.path == "/api/tasks":
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
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/analyze", parsed.path or "")
        if m:
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
            if replace_scan_root(p):
                self._send_json({"ok": True, "path": get_scan_root()})
            else:
                self._send_json(
                    {"ok": False, "error": "路径不存在或不是文件夹"},
                    400,
                )
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
        if not isinstance(fpath, str) or not fpath:
            self._send_json({"ok": False, "error": "missing path"}, 400)
            return
        if not os.path.exists(fpath) or not os.path.isfile(fpath):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        if not is_path_under_root(fpath):
            self._send_json({"ok": False, "error": "forbidden"}, 403)
            return
        try:
            os.remove(fpath)
            remove_media_thumb_cache(fpath)
            self._send_json({"ok": True})
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

# ===================== 前端页面 =====================
HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Media Browser v__APP_VERSION__</title>
<style id="mb-critical">html,body{min-height:100%}body{background:#0a0a0a;color:#e0e0e0;margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}</style>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #0a0a0a;
    color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: 40px;
}
header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: rgba(15,15,15,0.96);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid #2a2a2a;
    padding: 12px 20px;
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
}
.app-tabs {
    display: inline-flex;
    gap: 6px;
    margin-right: 6px;
}
.app-tab {
    background: #1b1b1b;
    border: 1px solid #3a3a3a;
    color: #aaa;
    padding: 7px 12px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
}
.app-tab.active {
    color: #fff;
    border-color: #0a84ff;
    background: rgba(10,132,255,0.18);
}
header .brand { display: flex; flex-direction: column; gap: 4px; margin-right: auto; min-width: 0; max-width: min(560px, 58vw); }
header h1 { font-size: 18px; color: #fff; letter-spacing: 0.5px; }
header .scan-root-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    color: #888;
    line-height: 1.35;
}
header .scan-root-row label { flex-shrink: 0; }
header #scanRootInput {
    flex: 1;
    min-width: 140px;
    max-width: min(420px, 42vw);
    font-family: ui-monospace, monospace;
    font-size: 11px;
    padding: 6px 10px;
}
header #applyScanRoot {
    flex-shrink: 0;
    font-size: 12px;
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid #0a84ff;
    background: rgba(10, 132, 255, 0.15);
    color: #0a84ff;
    cursor: pointer;
}
header #applyScanRoot:hover { background: rgba(10, 132, 255, 0.28); }
header #exitApp {
    font-size: 12px;
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid rgba(255, 69, 58, 0.45);
    background: rgba(255, 69, 58, 0.12);
    color: #ff6961;
    cursor: pointer;
    white-space: nowrap;
}
header #exitApp:hover { background: rgba(255, 69, 58, 0.22); }
header .app-ver { font-size: 11px; color: #555; margin-left: 6px; font-weight: normal; }
header input[type="text"] {
    background: #1e1e1e;
    border: 1px solid #3a3a3a;
    color: #eee;
    padding: 8px 14px;
    border-radius: 6px;
    width: 220px;
    font-size: 14px;
    transition: border-color 0.2s;
}
header input[type="text"]:focus { outline: none; border-color: #0a84ff; }
header select {
    background: #1e1e1e;
    border: 1px solid #3a3a3a;
    color: #eee;
    padding: 7px 10px;
    border-radius: 6px;
    font-size: 13px;
    cursor: pointer;
}
header select:focus { outline: none; border-color: #0a84ff; }
.filters button {
    background: #1e1e1e;
    border: 1px solid #3a3a3a;
    color: #999;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    transition: all 0.2s;
}
.filters button:hover { border-color: #555; color: #ccc; }
.filters button.active { background: #2a2a2a; color: #fff; border-color: #555; }
.folder-nav-hint {
    display: inline-block;
    font-size: 11px;
    color: #777;
    margin-left: 8px;
    vertical-align: middle;
    white-space: nowrap;
}
.folder-nav-hint kbd {
    display: inline-block;
    padding: 1px 5px;
    border: 1px solid #444;
    border-radius: 4px;
    background: #1a1a1a;
    color: #bbb;
    font-size: 10px;
    font-family: ui-monospace, monospace;
}
.review-reset-all {
    display: inline-block;
    font-size: 12px;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid #444;
    background: #222;
    color: #aaa;
    cursor: pointer;
}
.review-reset-all:hover { border-color: #666; color: #ddd; }
.mb-trash-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid #4a3a2a;
    background: #221a12;
    color: #ddb080;
    cursor: pointer;
}
.mb-trash-btn:hover { border-color: #6a5538; color: #f0d4a8; }
.mb-trash-badge {
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border-radius: 9px;
    background: #5c4030;
    color: #fff;
    font-size: 11px;
    font-weight: 600;
    line-height: 18px;
    text-align: center;
}
.mb-trash-btn[data-empty="1"] .mb-trash-badge { opacity: 0.45; }
.mb-trash-overlay {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 350;
    background: rgba(0,0,0,0.55);
    align-items: center;
    justify-content: center;
    padding: 16px;
}
.mb-trash-overlay.active { display: flex; }
.mb-trash-panel {
    width: min(720px, 100%);
    max-height: min(86vh, 640px);
    background: #141414;
    border: 1px solid #333;
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.mb-trash-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 16px;
    border-bottom: 1px solid #2a2a2a;
}
.mb-trash-head h2 { font-size: 16px; font-weight: 600; color: #eee; }
.mb-trash-head button {
    border: none;
    background: transparent;
    color: #888;
    font-size: 22px;
    line-height: 1;
    cursor: pointer;
    padding: 4px 8px;
}
.mb-trash-head button:hover { color: #fff; }
.mb-trash-desc {
    padding: 10px 16px;
    font-size: 12px;
    color: #999;
    line-height: 1.55;
    border-bottom: 1px solid #222;
}
.mb-trash-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px 16px;
    padding: 10px 16px;
    border-bottom: 1px solid #2a2a2a;
    background: #111;
}
.mb-trash-all-lbl {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: #ccc;
    cursor: pointer;
    user-select: none;
}
.mb-trash-all-lbl input { width: 16px; height: 16px; accent-color: #0a84ff; cursor: pointer; }
.mb-trash-toolbar #mbTrashDeleteSelected {
    font-size: 13px;
    padding: 7px 14px;
    border-radius: 6px;
    border: 1px solid #0a84ff;
    background: #0a84ff;
    color: #fff;
    cursor: pointer;
}
.mb-trash-toolbar #mbTrashDeleteSelected:disabled {
    opacity: 0.45;
    cursor: not-allowed;
    filter: none;
}
.mb-trash-sel-label { font-size: 12px; color: #777; flex: 1; min-width: 120px; }
.mb-trash-list-wrap {
    flex: 1;
    overflow: auto;
    padding: 8px 12px;
    font-size: 12px;
}
.mb-trash-row {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 10px;
    padding: 10px 8px;
    border-bottom: 1px solid #222;
    align-items: start;
}
.mb-trash-chk-lbl {
    display: flex;
    align-items: flex-start;
    padding-top: 2px;
    cursor: pointer;
}
.mb-trash-row-chk {
    width: 16px;
    height: 16px;
    accent-color: #0a84ff;
    cursor: pointer;
}
.mb-trash-path { color: #ccc; word-break: break-all; font-family: ui-monospace, monospace; font-size: 11px; }
.mb-trash-err { color: #c98; font-size: 11px; margin-top: 4px; }
.mb-trash-row-actions {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 8px;
}
.mb-trash-del-one {
    font-size: 12px;
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid #0a84ff;
    background: #0a84ff;
    color: #fff;
    cursor: pointer;
    white-space: nowrap;
}
.mb-trash-del-one:hover { filter: brightness(1.08); }
.mb-trash-dropq {
    font-size: 11px;
    padding: 0;
    border: none;
    background: transparent;
    color: #666;
    cursor: pointer;
    text-decoration: underline;
    white-space: nowrap;
}
.mb-trash-dropq:hover { color: #aaa; }
.mb-trash-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    padding: 14px 16px;
    border-top: 1px solid #2a2a2a;
}
.mb-trash-actions button.primary {
    background: #0a84ff;
    border-color: #0a84ff;
    color: #fff;
}
.mb-trash-empty { color: #666; padding: 24px 12px; text-align: center; }
.review-strip {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 8px;
    padding: 6px 8px;
    background: #111;
    border-radius: 6px;
    border: 1px solid #2a2a2a;
}
.review-tag {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
}
.review-tag.pending { background: #333; color: #aaa; }
.review-tag.kept { background: #1e3d2a; color: #8fd9a8; }
.review-to-pending {
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 4px;
    border: 1px solid #555;
    background: #1a1a1a;
    color: #ccc;
    cursor: pointer;
}
.review-to-pending:hover { border-color: #888; color: #fff; }
.gallery-review-btn {
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 4px;
    border: 1px solid #555;
    background: #1a1a1a;
    color: #ccc;
    cursor: pointer;
    margin-left: 10px;
}
.gallery-review-btn:hover { border-color: #888; color: #fff; }
.gallery-review-btn.kept { background: #1e3d2a; border-color: #2f5e42; color: #8fd9a8; }
.gallery-review-btn.pending { background: #333; border-color: #555; color: #aaa; }
.progress-bar {
    width: 100%;
    height: 3px;
    background: #1a1a1a;
    position: fixed;
    top: 0;
    left: 0;
    z-index: 101;
}
.progress-bar .fill {
    height: 100%;
    background: #0a84ff;
    width: 0%;
    transition: width 0.4s ease;
}
#container {
    display: grid;
    grid-template-columns: 1fr;
    gap: 24px;
    padding: 24px;
    max-width: 100%;
}
.empty-hint {
    max-width: 720px;
    margin: 32px auto 48px;
    padding: 28px 32px;
    background: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    color: #bbb;
    font-size: 14px;
    line-height: 1.65;
}
.empty-hint h2 {
    font-size: 17px;
    color: #eee;
    margin-bottom: 12px;
    font-weight: 600;
}
.empty-hint.empty-error { border-color: #5a2a2a; background: #1a1010; }
.empty-hint.empty-error h2 { color: #ff8a8a; }
.empty-hint ul { margin: 12px 0 12px 20px; color: #999; }
.empty-hint code { font-size: 12px; background: #1a1a1a; padding: 2px 6px; border-radius: 4px; color: #ccc; }
.empty-hint .cta-row { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.empty-hint button {
    background: #0a84ff;
    border: none;
    color: #fff;
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 13px;
    cursor: pointer;
}
.empty-hint button.secondary { background: #2a2a2a; color: #ddd; }
.analysis-panel {
    display: none;
    max-width: 1120px;
    margin: 18px auto 0;
    padding: 0 24px;
}
.analysis-panel.active { display: block; }
.analysis-card {
    background: #141414;
    border: 1px solid #262626;
    border-radius: 10px;
    padding: 14px;
    margin-bottom: 12px;
}
.analysis-card h3 { font-size: 16px; margin-bottom: 10px; }
.analysis-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin-bottom: 10px;
}
.analysis-row input[type="text"] {
    background: #1e1e1e;
    border: 1px solid #3a3a3a;
    color: #eee;
    padding: 8px 10px;
    border-radius: 6px;
    width: min(420px, 90vw);
}
.analysis-row button {
    background: #1e1e1e;
    border: 1px solid #444;
    color: #ccc;
    padding: 7px 12px;
    border-radius: 6px;
    cursor: pointer;
}
.analysis-row button.primary {
    background: #0a84ff;
    border-color: #0a84ff;
    color: #fff;
}
.analysis-note { color: #888; font-size: 12px; line-height: 1.5; }
.analysis-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
}
.analysis-table th, .analysis-table td {
    border-bottom: 1px solid #2a2a2a;
    padding: 8px 6px;
    text-align: left;
    vertical-align: top;
}
.analysis-table .ops button { margin-right: 6px; margin-bottom: 4px; }
.analysis-metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(260px, 1fr));
    gap: 10px 14px;
}
.analysis-metrics label {
    font-size: 12px;
    color: #999;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.analysis-metrics input {
    background: #1e1e1e;
    border: 1px solid #3a3a3a;
    color: #eee;
    border-radius: 6px;
    padding: 8px 10px;
}
.analysis-metrics .full { grid-column: 1 / -1; }
.work-card {
    width: 100%;
    max-width: 100%;
    background: #141414;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #222;
    transition: border-color 0.2s, transform 0.15s;
    position: relative;
}
.work-card:hover { border-color: #3a3a3a; transform: translateY(-1px); }
.work-card .chk {
    position: absolute;
    top: 8px;
    left: 8px;
    z-index: 10;
    width: 18px;
    height: 18px;
    cursor: pointer;
    accent-color: #0a84ff;
}
.work-content {
    display: flex;
    flex-direction: column;
    gap: 4px;
    background: #000;
    padding: 4px;
}
.video-row {
    display: flex;
    align-items: stretch;
    gap: 4px;
    background: #0a0a0a;
    border-radius: 4px;
    overflow: hidden;
}
.video-row .video-info {
    width: 120px;
    flex-shrink: 0;
    padding: 8px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
    background: #111;
}
.video-row .video-info .vname {
    font-size: 11px;
    color: #ccc;
    line-height: 1.3;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    word-break: break-all;
}
.video-row .video-info .vdur {
    font-size: 10px;
    color: #888;
}
.thumb-strip {
    flex: 1;
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 2px;
    background: #000;
}
.thumb-strip .thumb {
    aspect-ratio: 16/10;
    background: #111;
    position: relative;
    overflow: hidden;
    cursor: pointer;
}
.thumb-strip .thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: transform 0.25s ease;
}
.thumb-strip .thumb:hover img { transform: scale(1.06); }
.thumb-strip .thumb .badge {
    position: absolute;
    bottom: 5px;
    right: 5px;
    background: rgba(0,0,0,0.75);
    color: #fff;
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 4px;
    pointer-events: none;
    line-height: 1;
}
.thumb-strip .thumb .loader {
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, #111 25%, #1a1a1a 50%, #111 75%);
    background-size: 200% 100%;
    animation: shimmer 1.2s infinite;
    z-index: 1;
}
@keyframes shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
}
.thumb-strip .thumb img.loaded ~ .loader { display: none; }
.thumb-strip .thumb .ph-icon {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;
    color: #444;
    z-index: 2;
    pointer-events: none;
}
.thumb-strip .thumb img.loaded ~ .ph-icon { display: none; }
.image-row {
    display: flex;
    gap: 4px;
    background: #0a0a0a;
    border-radius: 4px;
    overflow: hidden;
    padding: 4px;
}
.image-row {
    cursor: pointer;
}
.image-row img {
    height: 120px;
    width: auto;
    object-fit: cover;
    border-radius: 4px;
    cursor: pointer;
    -webkit-user-drag: none;
    user-select: none;
}
.video-thumbs-panel {
    padding: 0 0 8px 0;
    border-bottom: none;
}
.video-thumbs-panel h4 {
    font-size: 11px;
    color: #888;
    margin: 0 0 8px 0;
    font-weight: 500;
}
.vt-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.vt-item {
    aspect-ratio: 16/10;
    background: #111;
    border-radius: 4px;
    overflow: hidden;
    cursor: pointer;
    position: relative;
    border: 2px solid transparent;
    transition: border-color 0.15s;
}
.vt-item:hover { border-color: #555; }
.vt-item.active { border-color: #0a84ff; }
.vt-item img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
}
.info {
    padding: 12px 14px;
}
.info .title-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
}
.info .title {
    font-size: 13px;
    color: #e0e0e0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
    cursor: pointer;
}
.info .title:hover { color: #0a84ff; text-decoration: underline; }
.info .meta {
    font-size: 11px;
    color: #777;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
}
.info .meta span { display: inline-flex; align-items: center; gap: 3px; }
#status {
    position: fixed;
    bottom: 16px;
    right: 16px;
    max-width: min(300px, 72vw);
    background: rgba(25,25,25,0.95);
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 12px;
    color: #999;
    border: 1px solid #2a2a2a;
    z-index: 90;
    backdrop-filter: blur(4px);
    pointer-events: none;
}
body.batch-open #status {
    bottom: 58px;
}
#backToTop {
    position: fixed;
    bottom: 60px;
    right: 16px;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: rgba(40,40,40,0.9);
    border: 1px solid #444;
    color: #ccc;
    font-size: 18px;
    cursor: pointer;
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 99;
    transition: all 0.2s;
}
#backToTop:hover { background: #333; color: #fff; }
#backToTop.show { display: flex; }
body.batch-open #backToTop.show { bottom: 118px; }

/* 批量操作栏 */
#batchBar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    background: rgba(20,20,20,0.96);
    border-top: 1px solid #333;
    padding: 10px 20px;
    display: none;
    align-items: center;
    gap: 12px;
    z-index: 130;
    backdrop-filter: blur(8px);
}
#batchBar.show { display: flex; flex-wrap: wrap; }
#batchBar .batch-info { font-size: 13px; color: #aaa; margin-right: auto; max-width: 100%; }
#batchBar .batch-hint { font-size: 11px; color: #666; font-weight: normal; }
#batchBar button {
    background: #1e1e1e;
    border: 1px solid #444;
    color: #ccc;
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 13px;
    cursor: pointer;
}
#batchBar button.primary { background: #0a84ff; border-color: #0a84ff; color: #fff; }

/* 模态框 */
.modal {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0);
    z-index: 200;
    transition: background 0.25s ease;
}
.modal.active { display: flex; background: rgba(0,0,0,0.94); }
.modal-body {
    display: flex;
    width: 100%;
    height: 100%;
    opacity: 0;
    transform: scale(0.96);
    transition: opacity 0.25s ease, transform 0.25s ease;
    --mb-gallery-sidebar: min(460px, 44vw);
    --mb-gallery-stage-maxw: min(52vw, calc(100vw - var(--mb-gallery-sidebar) - 96px));
    --mb-gallery-stage-maxh: min(56vh, calc(100vh - 190px));
}
.modal.active .modal-body {
    opacity: 1;
    transform: scale(1);
}
.modal-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    position: relative;
    padding: 20px;
}
.modal-main .video-wrap,
.modal-main #galleryVideoWrap {
    max-width: var(--mb-gallery-stage-maxw);
    max-height: var(--mb-gallery-stage-maxh);
}
.modal-main .video-wrap video,
.modal-main #galleryVideoWrap video {
    max-width: 100%;
    max-height: var(--mb-gallery-stage-maxh);
}
.modal-main .transcode-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0,0,0,0.82);
    color: #e8e8e8;
    font-size: 14px;
    z-index: 5;
    border-radius: 8px;
    pointer-events: none;
    text-align: center;
    padding: 12px;
    line-height: 1.4;
}
.modal-main video,
.modal-main img {
    max-width: min(100%, var(--mb-gallery-stage-maxw));
    max-height: var(--mb-gallery-stage-maxh);
    border-radius: 8px;
    box-shadow: 0 10px 40px rgba(0,0,0,0.5);
}
.modal-main .file-info {
    margin-top: 12px;
    font-size: 13px;
    color: #aaa;
    text-align: center;
    max-width: 80%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.modal-main .shortcut-hint {
    margin-top: 8px;
    font-size: 11px;
    color: #666;
}
.modal-nav {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    font-size: 36px;
    color: rgba(255,255,255,0.5);
    cursor: pointer;
    padding: 10px 16px;
    user-select: none;
    transition: color 0.2s;
    z-index: 10;
}
.modal-nav:hover { color: #fff; }
.modal-nav.prev { left: 10px; }
.modal-nav.next { right: 10px; }

.modal-sidebar {
    width: var(--mb-gallery-sidebar);
    flex-shrink: 0;
    background: #111;
    border-left: 1px solid #222;
    display: flex;
    flex-direction: column;
    padding: 0;
    gap: 0;
    overflow: hidden;
    min-height: 0;
    align-self: stretch;
}
.modal-sidebar-top {
    flex-shrink: 0;
    max-height: min(85vh, 1200px);
    overflow-y: auto;
    overflow-x: hidden;
    padding: 12px 12px 10px;
    border-bottom: 1px solid #252525;
    background: #111;
    z-index: 4;
}
.modal-sidebar-scroll {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 10px 12px 14px;
    -webkit-overflow-scrolling: touch;
}
.sidebar-thumb-dock:empty {
    display: none;
}
.sidebar-thumb-dock {
    margin-top: 6px;
}
.sidebar-thumb-dock .vt-list {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
}
.modal-sidebar h3 {
    font-size: 13px;
    color: #888;
    font-weight: 500;
    margin: 0 0 2px 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.file-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.file-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px;
    border-radius: 6px;
    cursor: pointer;
    border: 1px solid transparent;
    transition: background 0.15s;
}
.file-item:hover { background: #1a1a1a; }
.file-item.active { background: #1a2a3a; border-color: #0a84ff; }
.file-item .thumb-wrap {
    width: 48px;
    height: 36px;
    border-radius: 4px;
    background: #000;
    flex-shrink: 0;
    overflow: hidden;
    position: relative;
}
.file-item .thumb-wrap img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
}
.file-item .thumb-wrap .ph {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    color: #444;
}
.file-item .thumb-wrap img.loaded + .ph { display: none; }
.file-item .file-meta {
    flex: 1;
    min-width: 0;
}
.file-item .file-name {
    font-size: 11px;
    color: #ccc;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.file-item .file-sub {
    font-size: 10px;
    color: #666;
    margin-top: 2px;
}
.file-load-more {
    text-align: center;
    padding: 8px;
    font-size: 12px;
    color: #666;
    cursor: pointer;
    border-radius: 6px;
    border: 1px dashed #333;
}
.file-load-more:hover { color: #aaa; border-color: #555; }

.modal-close {
    position: absolute;
    top: 12px;
    right: calc(var(--mb-gallery-sidebar) + 18px);
    font-size: 28px;
    color: rgba(255,255,255,0.5);
    cursor: pointer;
    z-index: 20;
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    transition: all 0.2s;
}
.modal-close:hover { color: #fff; background: rgba(255,255,255,0.1); }
.modal-fs {
    position: absolute;
    top: 12px;
    right: calc(var(--mb-gallery-sidebar) + 62px);
    font-size: 20px;
    color: rgba(255,255,255,0.5);
    cursor: pointer;
    z-index: 20;
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    transition: all 0.2s;
    user-select: none;
}
.modal-fs:hover { color: #fff; background: rgba(255,255,255,0.1); }
.img-wrap {
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    max-width: var(--mb-gallery-stage-maxw);
    max-height: var(--mb-gallery-stage-maxh);
    width: auto;
    height: auto;
}
.img-wrap img {
    transition: transform 0.1s ease-out;
    transform-origin: center center;
    user-select: none;
}

@media (max-width: 900px) {
    .modal-body {
        --mb-gallery-sidebar: 0px;
        --mb-gallery-stage-maxw: min(92vw, calc(100vw - 20px));
        --mb-gallery-stage-maxh: min(64vh, calc(100vh - 160px));
    }
    .modal-sidebar { display: none; }
    .modal-close { right: 12px; }
    .modal-fs { right: 60px; }
    #container { padding: 12px; }
    header input[type="text"] { width: 100%; }
    header .brand { max-width: 100%; }
}
.mb-review-shell { width: 100%; }
.mb-review-layout { display: flex; flex-direction: column; align-items: stretch; width: 100%; min-height: 0; }
.mb-works-column { position: relative; min-width: 0; flex: 1; transition: width 0.22s ease, min-width 0.22s ease, opacity 0.18s ease; }
.mb-works-col-toggle {
    display: none; position: sticky; top: 72px; z-index: 90; float: right; margin: 4px 8px 0 0;
    width: 36px; height: 36px; border-radius: 8px; border: 1px solid #3a3a3a; background: #1a1a1a; color: #ccc;
    cursor: pointer; font-size: 16px; line-height: 1; box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}
.mb-review-secondary {
    display: none; border-left: 1px solid #222; padding: 16px 20px; color: #888; font-size: 13px; line-height: 1.55;
    background: linear-gradient(180deg, #101010 0%, #0a0a0a 100%); min-height: 120px;
}
.mb-review-secondary-card { max-width: 420px; }
.mb-hint-title { color: #bbb; font-weight: 600; margin-bottom: 8px; font-size: 14px; }
.mb-v-spacer { width: 100%; pointer-events: none; flex-shrink: 0; }
.mb-card-ctx {
    position: fixed; z-index: 400; min-width: 200px; background: #1a1a1a; border: 1px solid #3a3a3a; border-radius: 10px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.55); padding: 6px 0; display: none;
}
.mb-card-ctx button {
    display: block; width: 100%; text-align: left; padding: 10px 16px; border: none; background: transparent;
    color: #e0e0e0; font-size: 14px; cursor: pointer;
}
.mb-card-ctx button:hover { background: #2a2a2a; }
.mb-mobile-tabbar {
    display: none; position: fixed; left: 0; right: 0; bottom: 0; z-index: 200;
    height: calc(52px + env(safe-area-inset-bottom, 0px)); padding-bottom: env(safe-area-inset-bottom, 0px);
    background: rgba(14,14,14,0.96); border-top: 1px solid #2a2a2a; backdrop-filter: blur(12px);
    justify-content: space-around; align-items: center;
}
.mb-mobile-tabbar button {
    flex: 1; border: none; background: transparent; color: #888; font-size: 12px; padding: 8px 4px; cursor: pointer;
}
.mb-mobile-tabbar button.active { color: #0a84ff; font-weight: 600; }
@media (min-width: 768px) and (max-width: 1023px) {
    .mb-review-layout { flex-direction: row; align-items: stretch; }
    .mb-works-column { width: 42%; max-width: 440px; flex: 0 0 auto; border-right: 1px solid #222; }
    .mb-works-column.mb-collapsed { width: 0 !important; min-width: 0 !important; max-width: 0 !important; opacity: 0; overflow: hidden; border: none; padding: 0; }
    .mb-works-col-toggle { display: block; }
    .mb-review-secondary { display: flex; flex: 1; align-items: flex-start; }
}
@media (min-width: 1024px) {
    .mb-review-layout { flex-direction: row; align-items: flex-start; }
    .mb-works-column { width: min(420px, 38vw); flex: 0 0 auto; border-right: 1px solid #222; }
    .mb-review-secondary { display: flex; flex: 1; min-height: calc(100vh - 120px); align-items: flex-start; }
    .mb-works-col-toggle { display: none; }
}
@media (max-width: 767px) {
    body { padding-bottom: calc(56px + env(safe-area-inset-bottom, 0px)); }
    body.mb-review-tab .mb-mobile-tabbar { display: flex; }
    #backToTop { bottom: calc(64px + env(safe-area-inset-bottom, 0px)); }
    .folder-nav-hint { display: none; }
    .mb-review-secondary { display: none !important; }
}
</style>
</head>
<body>
<div class="progress-bar"><div class="fill" id="progressFill"></div></div>
<header id="headerBar">
    <div class="brand">
        <h1>📁 Media Browser <span class="app-ver">v__APP_VERSION__</span></h1>
        <div class="scan-root-row" title="可填写本机任意目录；无需重启。启动默认仍可由 MB_ROOT_DIR 决定。">
            <label for="scanRootInput">扫描根目录</label>
            <input type="text" id="scanRootInput" value="__MB_ROOT_DIR__" spellcheck="false" autocomplete="off" />
            <button type="button" id="applyScanRoot">应用并扫描</button>
        </div>
    </div>
    <div class="app-tabs" aria-label="一级菜单">
        <button type="button" id="tabReview" class="app-tab active">视频审阅</button>
        <button type="button" id="tabAnalysis" class="app-tab">视频分析任务</button>
    </div>
    <input type="text" id="search" placeholder="搜索作品或文件名..." autocomplete="off">
    <select id="sortSelect">
        <option value="name">按名称</option>
        <option value="files">按文件数</option>
        <option value="size">按大小</option>
        <option value="duration">按时长</option>
        <option value="mtime">按修改时间</option>
    </select>
    <div class="filters">
        <button class="active" data-filter="all">全部</button>
        <button data-filter="video">有视频</button>
        <button data-filter="image">有图片</button>
        <button data-filter="pending">仅待审</button>
    </div>
    <span class="folder-nav-hint" title="「视频审阅」页、焦点不在输入框内；画廊打开时切作品；Windows 为 Ctrl+← / Ctrl+→">作品：<kbd>⌘←</kbd> 上一个 · <kbd>⌘→</kbd> 下一个（画廊内切作品；列表内滚卡片）</span>
    <button type="button" id="resetAllReviewTags" class="review-reset-all" title="将所有作品的待审/保留标记清空为「待审」">标记全重置</button>
    <button type="button" id="mbTrashBtn" class="mb-trash-btn" data-empty="1" title="删除失败时暂存于此，便于对指定文件再次删除">废纸篓 <span id="mbTrashCount" class="mb-trash-badge">0</span></button>
    <button type="button" id="exitApp" title="停止本地服务并退出 Media Browser（终端模式将返回提示符）">退出应用</button>
</header>

<section id="analysisPanel" class="analysis-panel" aria-live="polite">
    <div class="analysis-card">
        <h3>视频分析任务</h3>
        <div class="analysis-row">
            <input type="text" id="taskNameInput" placeholder="任务名称（例如：2024冰岛批次）" autocomplete="off">
            <button type="button" class="primary" id="createTaskBtn">用当前勾选创建任务</button>
            <button type="button" id="refreshTasksBtn">刷新任务列表</button>
        </div>
        <div class="analysis-note">
            <b>推荐流程：</b>本机运行 Ollama 并拉取视觉模型（如 <code>ollama pull llava</code>）→ 创建任务 → 点「AI分析」（默认 <code>http://127.0.0.1:11434</code>，可用 <code>MB_OLLAMA_HOST</code> / <code>MB_OLLAMA_MODEL</code> 配置）→ 在下方表格核对时间/地点/事件与标签、修改短语 → 勾选「准确」并保存 → 再点「预览」「执行」重命名。<br>
            若未跑 AI，仍可用文件夹路径的启发式命名生成预览。<br>
            <b>范围规则：</b>仅同目录改名，禁止跨目录移动；执行前预览并生成映射备份；文件名末尾自动追加序号。
        </div>
    </div>
    <div class="analysis-card">
        <table class="analysis-table">
            <thead>
                <tr><th>任务</th><th>状态</th><th>文件数</th><th>映射</th><th>操作</th></tr>
            </thead>
            <tbody id="analysisTaskRows">
                <tr><td colspan="5" style="color:#777;">暂无任务</td></tr>
            </tbody>
        </table>
    </div>
    <div class="analysis-card" id="insightCard">
        <h3>AI 标签核对（确认后再重命名）</h3>
        <div class="analysis-row" style="flex-wrap:wrap;gap:8px;">
            <span id="insightTaskLabel" class="analysis-note">先点某一行的「标签」打开此表，或创建任务后点「AI分析」。</span>
            <button type="button" id="loadInsightBtn">刷新本表</button>
            <button type="button" class="primary" id="saveConfirmBtn">保存确认</button>
        </div>
        <div id="analyzeProgress" class="analysis-note" style="display:none;margin-top:6px;"></div>
        <div class="analysis-note" style="margin-top:4px;color:#888;font-size:12px;">提示：点「AI分析」会自动切到本页；左侧列为<strong>视频缩略图</strong>（点击在新标签页播放），请对照画面核对标签与短语。若启动失败，请先在本机运行 <code>ollama serve</code>。</div>
        <div id="insightTableWrap" style="overflow:auto;max-height:460px;margin-top:8px;"></div>
    </div>
    <div class="analysis-card">
        <h3>预览（最近一次）</h3>
        <div id="analysisPreview" class="analysis-note">尚未生成预览。</div>
    </div>
    <div class="analysis-card">
        <h3>效果打分（人工认可率 + Top-K 命中率）</h3>
        <div class="analysis-note" style="margin-bottom:8px;">
            怎么打分（简单版）：<br>
            1) 先在下拉里选一个任务；<br>
            2) 人工复核总数 = 你实际检查了多少条，人工认可数 = 其中你认可的条数；<br>
            3) Top-K 总样本/命中数按同一批样本填写，点“保存评测”即可。
        </div>
        <div class="analysis-row">
            <select id="evalTaskSelect" title="请选择任务"></select>
            <input type="number" id="evalReviewedInput" min="0" placeholder="人工复核总数">
            <input type="number" id="evalApprovedInput" min="0" placeholder="人工认可数">
            <input type="number" id="evalTopkTotalInput" min="0" placeholder="Top-K 总样本">
            <input type="number" id="evalTopkHitInput" min="0" placeholder="Top-K 命中数">
            <button type="button" class="primary" id="saveEvalBtn">保存评测</button>
        </div>
        <div id="evalResult" class="analysis-note">还没有保存打分。</div>
    </div>
</section>

<div id="reviewShell" class="mb-review-shell">
<div class="mb-review-layout" id="reviewLayout">
<aside id="worksColumn" class="mb-works-column">
<button type="button" id="worksColToggle" class="mb-works-col-toggle" aria-expanded="true" title="折叠/展开作品列表">⟨</button>
<div id="container"></div>
<div id="emptyHint" class="empty-hint" style="display:none;" aria-live="polite"></div>
<div id="status">准备扫描...</div>
</aside>
<div id="reviewSecondary" class="mb-review-secondary" aria-hidden="true">
<div class="mb-review-secondary-card">
<div class="mb-hint-title">布局说明</div>
<p>桌面：左侧作品列表与右侧提示栏分栏。平板：点 ⟨ 可折叠左侧列表。手机：底部「作品 / 画廊 / 设置」切换；画廊内横划切文件，长横划切作品；双指捏合缩放图片或视频画面。</p>
</div>
</div>
</div>
</div>

<nav id="mobileTabbar" class="mb-mobile-tabbar" aria-label="底部导航">
<button type="button" data-mtab="works" class="active">作品</button>
<button type="button" data-mtab="gallery">画廊</button>
<button type="button" data-mtab="settings">设置</button>
</nav>

<div id="cardCtxMenu" class="mb-card-ctx" role="menu" aria-hidden="true">
<button type="button" role="menuitem" data-ctx-act="open">打开画廊</button>
<button type="button" role="menuitem" data-ctx-act="finder">在 Finder 中打开</button>
<button type="button" role="menuitem" data-ctx-act="rename">重命名说明</button>
<button type="button" role="menuitem" data-ctx-act="rescan">重新扫描标签</button>
<button type="button" role="menuitem" data-ctx-act="delete">删除作品内全部文件…</button>
</div>

<script type="text/plain" id="mbDeferredCss">.mb-review-secondary{content-visibility:auto}.analysis-panel .analysis-table{content-visibility:auto}</script>

<button id="backToTop" onclick="scrollToTop()" title="返回顶部">↑</button>

<!-- 批量操作栏 -->
<div id="batchBar">
    <span class="batch-info"><b id="batchCount">0</b> 个已勾选 <span class="batch-hint">· 可批量改「保留/待审」或在 Finder 中打开所在文件夹</span></span>
    <button onclick="selectAllVisible()">全选当前列表</button>
    <button onclick="clearSelection()">取消勾选</button>
    <button class="primary" onclick="batchMarkReview('kept')">标为保留</button>
    <button onclick="batchMarkReview('pending')">标为待审</button>
    <button onclick="batchOpenFinder()">在 Finder 中打开</button>
</div>

<div id="mbTrashOverlay" class="mb-trash-overlay" aria-hidden="true">
    <div class="mb-trash-panel" role="dialog" aria-labelledby="mbTrashTitle">
        <div class="mb-trash-head">
            <h2 id="mbTrashTitle">废纸篓（待删文件）</h2>
            <button type="button" id="mbTrashClose" aria-label="关闭">&times;</button>
        </div>
        <p class="mb-trash-desc">以下路径曾因权限、磁盘只读、文件被占用等原因<strong>删除失败</strong>，已自动记入本队列。你要做的仍是<strong>删除这些指定文件</strong>：可勾选多项后点「删除所选」，或逐条点「删除此文件」，或在本机解除占用后点「全部再次删除」。若你已在访达中手动删掉文件，对应行会在刷新后自动消失；只有当你<strong>不想</strong>再尝试删除某条时，才用该行「移出队列」。</p>
        <div class="mb-trash-toolbar">
            <label class="mb-trash-all-lbl"><input type="checkbox" id="mbTrashSelectAll" title="全选当前列表"> 全选</label>
            <button type="button" id="mbTrashDeleteSelected" disabled>删除所选</button>
            <span id="mbTrashSelectedLabel" class="mb-trash-sel-label" aria-live="polite"></span>
        </div>
        <div id="mbTrashListWrap" class="mb-trash-list-wrap"></div>
        <div class="mb-trash-actions">
            <button type="button" class="primary" id="mbTrashRetryAll">全部再次删除</button>
        </div>
    </div>
</div>

<!-- 画廊模态框 -->
<div class="modal" id="modal">
    <div class="modal-body">
        <div class="modal-main" id="modalMain">
            <div class="modal-close" onclick="closeModal()">&times;</div>
            <div class="modal-fs" onclick="toggleGalleryFullscreen()" title="全屏">⛶</div>
            <div class="modal-nav prev" onclick="navigate(-1)">&#10094;</div>
            <div class="modal-nav next" onclick="navigate(1)">&#10095;</div>
            <div id="modalMedia"></div>
            <div class="file-info" id="modalFileInfo"></div>
            <div class="shortcut-hint" id="shortcutHint">← → 翻页 · ESC 关闭 · 空格 播放/暂停 · ⌘I 删当前 · ⌘⇧I 删本作品 · ⌘← / ⌘→ 上/下一作品（列表与画廊）</div>
        </div>
        <div class="modal-sidebar" id="modalSidebar">
            <div class="modal-sidebar-top">
                <h3 id="sidebarTitle">作品文件</h3>
                <div id="sidebarThumbDock" class="sidebar-thumb-dock" aria-label="当前视频帧缩略图"></div>
            </div>
            <div class="modal-sidebar-scroll" id="sidebarFileScroll">
                <div id="fileList" class="file-list"></div>
            </div>
        </div>
    </div>
</div>

<script>
const THUMB_COUNT = __THUMB_COUNT__;
const container = document.getElementById('container');
const statusEl = document.getElementById('status');
const progressFill = document.getElementById('progressFill');
const searchInput = document.getElementById('search');
const sortSelect = document.getElementById('sortSelect');
const backToTopBtn = document.getElementById('backToTop');
const batchBar = document.getElementById('batchBar');
const batchCountEl = document.getElementById('batchCount');
const reviewShell = document.getElementById('reviewShell');
const worksColumn = document.getElementById('worksColumn');
const mobileTabbar = document.getElementById('mobileTabbar');
const cardCtxMenu = document.getElementById('cardCtxMenu');

const MB_LAZY_PLACEHOLDER = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
const MB_V_THRESHOLD = 500;
const MB_V_ROW = 280;
let mbVirtual = { active: false, list: [], anchor: 0 };
let mbLastGalleryWorkId = null;
let mbThumbIO = null;
let mbVirtScrollRaf = 0;
let mbCtxWork = null;

function mbRic(cb) {
    if (window.requestIdleCallback) requestIdleCallback(cb, { timeout: 900 });
    else setTimeout(cb, 40);
}
mbRic(function () {
    var el = document.getElementById('mbDeferredCss');
    if (!el || !el.textContent) return;
    var s = document.createElement('style');
    s.textContent = el.textContent;
    document.head.appendChild(s);
});

let allWorks = [];
let filterType = 'all';
let sortKey = 'name';
let since = 0;
let selectedIds = new Set();
let displayedIds = new Set();
const BATCH_STEP = 12;
let renderOffset = 0;

let galleryState = { workId: null, itemIdx: 0 };
let sidebarOffset = 0;
let sidebarLimit = 40;
let lastEnumError = null;
let scanPollDone = false;
let pollGen = 0;
let activeTab = 'review';
let insightTaskId = null;
let insightPollTimer = null;

document.body.classList.add('mb-review-tab');

function fmtSize(bytes) {
    if (!bytes) return '0 B';
    const units = ['B','KB','MB','GB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length-1) { bytes /= 1024; i++; }
    return bytes.toFixed(i>0?1:0) + ' ' + units[i];
}
function fmtDuration(sec) {
    if (!sec || sec <= 0) return '';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    return h > 0 ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
}
function escapeHtml(t) {
    return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

const mbTrashBtn = document.getElementById('mbTrashBtn');
const mbTrashCount = document.getElementById('mbTrashCount');
const mbTrashOverlay = document.getElementById('mbTrashOverlay');
const mbTrashListWrap = document.getElementById('mbTrashListWrap');
let mbTrashPollTs = 0;
let mbTrashItemsCache = [];

async function mbRefreshTrashBadge(force) {
    const now = Date.now();
    if (!force && (now - mbTrashPollTs) < 5000) return;
    mbTrashPollTs = now;
    try {
        const res = await fetch('/api/delete-trash', { cache: 'no-store' });
        const d = await res.json();
        const n = (d && typeof d.count === 'number') ? d.count : 0;
        if (mbTrashCount) mbTrashCount.textContent = String(n);
        if (mbTrashBtn) mbTrashBtn.setAttribute('data-empty', n ? '0' : '1');
    } catch (err) {}
}

function mbCloseTrashPanel() {
    if (!mbTrashOverlay) return;
    mbTrashOverlay.classList.remove('active');
    mbTrashOverlay.setAttribute('aria-hidden', 'true');
}

function mbRescanWorksAfterTrashDelete() {
    pollGen++;
    since = 0;
    scanPollDone = false;
    allWorks = [];
    lastEnumError = null;
    progressFill.style.width = '0%';
    progressFill.style.background = '';
    sortAndRenderAll();
    poll();
}

function mbTrashUpdateSelectionUi() {
    const wrap = mbTrashListWrap;
    const sa = document.getElementById('mbTrashSelectAll');
    const delBtn = document.getElementById('mbTrashDeleteSelected');
    const label = document.getElementById('mbTrashSelectedLabel');
    if (!wrap || !delBtn) return;
    const chks = wrap.querySelectorAll('.mb-trash-row-chk');
    let n = 0;
    chks.forEach(function (c) { if (c.checked) n++; });
    delBtn.disabled = n === 0;
    if (label) label.textContent = n ? ('已选 ' + n + ' 项') : '';
    if (sa) {
        if (!chks.length) {
            sa.checked = false;
            sa.indeterminate = false;
        } else {
            let c = 0;
            chks.forEach(function (x) { if (x.checked) c++; });
            sa.checked = c === chks.length;
            sa.indeterminate = c > 0 && c < chks.length;
        }
    }
}

async function mbTrashDeleteSelectedAction() {
    if (!mbTrashListWrap) return;
    const paths = [];
    mbTrashListWrap.querySelectorAll('.mb-trash-row-chk:checked').forEach(function (c) {
        const ix = parseInt(c.getAttribute('data-trash-sel-ix'), 10);
        const row = mbTrashItemsCache[ix];
        if (row && row.path) paths.push(row.path);
    });
    if (!paths.length) return;
    if (!confirm('将永久删除已选的 ' + paths.length + ' 个文件，不可恢复。确定？')) return;
    try {
        const res = await fetch('/api/delete-trash/delete-selected', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({ paths }),
        });
        const d = await res.json();
        if (!d.ok) { alert(d.error || '操作失败'); return; }
        let msg = '已删除 ' + (d.deleted || 0) + ' 个。';
        if (d.skipped && d.skipped.length) msg += ' 跳过 ' + d.skipped.length + ' 项。';
        if (d.errors && d.errors.length) msg += ' 仍有失败 ' + d.errors.length + ' 项。';
        alert(msg);
        await mbOpenTrashPanel();
        mbRefreshTrashBadge(true);
        if ((d.deleted || 0) > 0) mbRescanWorksAfterTrashDelete();
    } catch (e) {
        alert(e.message || String(e));
    }
}

function mbRenderTrashList(items) {
    if (!mbTrashListWrap) return;
    mbTrashItemsCache = items || [];
    if (!items || !items.length) {
        mbTrashListWrap.innerHTML = '<div class="mb-trash-empty">暂无记录。删除失败时会加入此队列，便于对<strong>指定文件</strong>再次执行删除。</div>';
        mbTrashUpdateSelectionUi();
        return;
    }
    let html = '';
    for (let i = 0; i < items.length; i++) {
        const it = items[i];
        const p = it.path || '';
        const err = it.last_error || '';
        html += '<div class="mb-trash-row">';
        html += '<label class="mb-trash-chk-lbl"><input type="checkbox" class="mb-trash-row-chk" data-trash-sel-ix="' + i + '" aria-label="选中以加入批量删除"></label>';
        html += '<div><div class="mb-trash-path">' + escapeHtml(p) + '</div>';
        if (err) html += '<div class="mb-trash-err">' + escapeHtml(err) + '</div>';
        html += '</div><div class="mb-trash-row-actions">';
        html += '<button type="button" class="mb-trash-del-one" data-trash-del-idx="' + i + '">删除此文件</button>';
        html += '<button type="button" class="mb-trash-dropq" data-trash-rm-idx="' + i + '">移出队列</button>';
        html += '</div></div>';
    }
    mbTrashListWrap.innerHTML = html;
    mbTrashListWrap.querySelectorAll('.mb-trash-row-chk').forEach(function (c) {
        c.addEventListener('change', mbTrashUpdateSelectionUi);
    });
    mbTrashListWrap.querySelectorAll('[data-trash-del-idx]').forEach(function (btn) {
        btn.onclick = async function () {
            const ix = parseInt(btn.getAttribute('data-trash-del-idx'), 10);
            const row = mbTrashItemsCache[ix];
            if (!row || !row.path) return;
            if (!confirm('确定永久删除该文件？\\n' + row.path)) return;
            try {
                const res = await fetch('/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json; charset=utf-8' },
                    body: JSON.stringify({ path: row.path }),
                });
                const d = await res.json();
                if (!d.ok) {
                    let msg = '删除失败: ' + (d.error || '未知错误');
                    if (d.queued) msg += '\\n已更新废纸篓记录。';
                    alert(msg);
                    mbRefreshTrashBadge(true);
                    await mbOpenTrashPanel();
                    return;
                }
                await mbOpenTrashPanel();
                mbRefreshTrashBadge(true);
                mbRescanWorksAfterTrashDelete();
            } catch (e) {
                alert(e.message || String(e));
            }
        };
    });
    mbTrashListWrap.querySelectorAll('[data-trash-rm-idx]').forEach(function (btn) {
        btn.onclick = async function () {
            const ix = parseInt(btn.getAttribute('data-trash-rm-idx'), 10);
            const row = mbTrashItemsCache[ix];
            if (!row || !row.path) return;
            if (!confirm('从队列中移除此路径？\\n不会删除磁盘上的文件；仅在你确定不再通过本工具删除该项时使用。')) return;
            try {
                const res = await fetch('/api/delete-trash/remove', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json; charset=utf-8' },
                    body: JSON.stringify({ paths: [row.path] }),
                });
                const d = await res.json();
                if (!d.ok) { alert(d.error || '移除失败'); return; }
                await mbOpenTrashPanel();
                mbRefreshTrashBadge(true);
            } catch (e) { alert(e.message || String(e)); }
        };
    });
    mbTrashUpdateSelectionUi();
}

async function mbOpenTrashPanel() {
    if (!mbTrashOverlay) return;
    mbTrashOverlay.classList.add('active');
    mbTrashOverlay.setAttribute('aria-hidden', 'false');
    try {
        const res = await fetch('/api/delete-trash', { cache: 'no-store' });
        const d = await res.json();
        mbRenderTrashList((d && d.items) ? d.items : []);
        mbRefreshTrashBadge(true);
    } catch (e) {
        if (mbTrashListWrap) mbTrashListWrap.innerHTML = '<div class="mb-trash-empty">加载失败</div>';
        mbTrashUpdateSelectionUi();
    }
}

async function mbTrashRetryAllAction() {
    if (!confirm('将依次再次尝试永久删除队列中的全部文件，不可恢复。确定？')) return;
    try {
        const res = await fetch('/api/delete-trash/retry-all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: '{}',
        });
        const d = await res.json();
        if (!d.ok) { alert(d.error || '操作失败'); return; }
        let msg = '已删除 ' + (d.deleted || 0) + ' 个；剩余 ' + (d.remaining || 0) + ' 个。';
        if (d.errors && d.errors.length) msg += '\\n仍有失败项，请查看清单。';
        alert(msg);
        await mbOpenTrashPanel();
        mbRefreshTrashBadge(true);
        if ((d.deleted || 0) > 0) mbRescanWorksAfterTrashDelete();
    } catch (e) {
        alert(e.message || String(e));
    }
}

const TRANSCODE_VIDEO_EXTS = new Set(['.avi','.ts','.mts','.m2ts','.wmv','.vob','.flv']);
function buildVideoPlayUrl(filePath) {
    const i = filePath.lastIndexOf('.');
    const ext = i >= 0 ? filePath.slice(i).toLowerCase() : '';
    if (TRANSCODE_VIDEO_EXTS.has(ext)) {
        return '/play?path=' + encodeURIComponent(filePath);
    }
    return '/file?path=' + encodeURIComponent(filePath);
}
window.insightRowThumbError = function(ev) {
    const img = ev && ev.target;
    if (!img || img.tagName !== 'IMG') return;
    img.onerror = null;
    img.src = 'data:image/svg+xml,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect fill="#2a2a2a" width="100%" height="100%"/>' +
        '<text x="50%" y="50%" fill="#888" font-size="10" text-anchor="middle" dy=".35em">无缩略图</text></svg>'
    );
};
function getWorkReviewTag(workId) {
    try {
        const o = JSON.parse(localStorage.getItem('mb_review_tags') || '{}');
        return o[workId] === 'kept' ? 'kept' : 'pending';
    } catch (e) { return 'pending'; }
}
function buildReviewStripHtml(workId) {
    const kept = getWorkReviewTag(workId) === 'kept';
    const btn = kept ? '<button type="button" class="review-to-pending" data-work-id="' + escapeHtml(workId) + '">标为待审</button>' : '';
    const cls = kept ? 'kept' : 'pending';
    const txt = kept ? '保留' : '待审';
    return '<div class="review-strip"><span class="review-tag ' + cls + '">' + txt + '</span>' + btn + '</div>';
}
function setWorkReviewTag(workId, tag) {
    try {
        const o = JSON.parse(localStorage.getItem('mb_review_tags') || '{}');
        o[workId] = tag;
        localStorage.setItem('mb_review_tags', JSON.stringify(o));
    } catch (e) {}
    const card = document.querySelector('.work-card[data-id="' + CSS.escape(workId) + '"]');
    if (card) {
        const strip = card.querySelector('.review-strip');
        if (strip) strip.outerHTML = buildReviewStripHtml(workId);
    }
    // 如果画廊正打开该作品，同步更新画廊按钮
    if (galleryState.workId === workId) {
        updateGalleryReviewBtn(workId);
    }
}
function setWorkReviewPending(workId) {
    setWorkReviewTag(workId, 'pending');
}
function markWorkOpenedKept(workId) {
    setWorkReviewTag(workId, 'kept');
}
function toggleWorkReview(workId) {
    const current = getWorkReviewTag(workId);
    setWorkReviewTag(workId, current === 'kept' ? 'pending' : 'kept');
}
function updateGalleryReviewBtn(workId) {
    const btn = document.getElementById('galleryReviewBtn');
    if (!btn) return;
    const kept = getWorkReviewTag(workId) === 'kept';
    btn.textContent = kept ? '保留' : '待审';
    btn.className = 'gallery-review-btn ' + (kept ? 'kept' : 'pending');
    btn.title = kept ? '点击标为待审' : '点击标为保留';
}
function resetAllReviewTags() {
    if (!confirm('将所有作品的标记恢复为「待审」？')) return;
    localStorage.removeItem('mb_review_tags');
    sortAndRenderAll();
}
function buildThumbUrl(cachePath) {
    if (!cachePath) return '';
    const parts = String(cachePath).split(/[\\/]+/).filter(Boolean);
    if (parts.length < 2) return '';
    const hash = parts[parts.length-2];
    const name = parts[parts.length-1];
    return `/thumb/${hash}/${name}`;
}
function buildFileUrl(path) {
    return '/file?path=' + encodeURIComponent(path);
}

function switchTab(tab) {
    activeTab = tab === 'analysis' ? 'analysis' : 'review';
    const reviewVisible = activeTab === 'review';
    const reviewDisplay = reviewVisible ? '' : 'none';
    container.style.display = reviewDisplay;
    document.getElementById('emptyHint').style.display = reviewVisible ? '' : 'none';
    statusEl.style.display = reviewVisible ? '' : 'none';
    backToTopBtn.style.display = reviewVisible ? '' : 'none';
    batchBar.style.display = reviewVisible ? '' : 'none';
    if (reviewShell) reviewShell.style.display = reviewDisplay;
    document.body.classList.toggle('mb-review-tab', reviewVisible);
    document.getElementById('analysisPanel').classList.toggle('active', !reviewVisible);
    document.getElementById('tabReview').classList.toggle('active', reviewVisible);
    document.getElementById('tabAnalysis').classList.toggle('active', !reviewVisible);
    if (!reviewVisible) loadTaskList();
}

async function loadTaskList() {
    const rows = document.getElementById('analysisTaskRows');
    const evalSel = document.getElementById('evalTaskSelect');
    try {
        const r = await fetch('/api/tasks', { cache: 'no-store' });
        const data = await r.json();
        if (!data.ok) throw new Error(data.error || '加载任务失败');
        if (evalSel) {
            const oldVal = evalSel.value || '';
            evalSel.innerHTML = '<option value="">选择任务后再保存评分</option>' + (data.tasks || []).map(t =>
                `<option value="${t.id}">${escapeHtml(t.name)} (${t.id})</option>`
            ).join('');
            if (oldVal && (data.tasks || []).some(t => t.id === oldVal)) {
                evalSel.value = oldVal;
            } else if ((data.tasks || []).length > 0) {
                // 用户确认：默认选中最新任务
                evalSel.value = data.tasks[0].id;
            }
        }
        if (!data.tasks || data.tasks.length === 0) {
            rows.innerHTML = '<tr><td colspan="5" style="color:#777;">暂无任务</td></tr>';
            return;
        }
        rows.innerHTML = data.tasks.map(t => {
            const map = t.mapping_file ? `<a href="#" onclick="event.preventDefault(); copyText(${JSON.stringify(t.mapping_file)});">复制路径</a>` : '-';
            const e = t.eval || {};
            const evalText = `人工通过率 ${Math.round(Number(e.human_pass_rate || 0))}% / Top-K ${Math.round(Number(e.topk_hit_rate || 0))}%`;
            const an = t.analyze || {};
            const anNote = (an.state === 'running')
                ? `<div style="color:#8cf;font-size:10px;">AI ${Number(an.done || 0)}/${Number(an.total || 0)}</div>`
                : (t.needs_confirm ? `<div style="color:#fa8;font-size:10px;">待确认标签</div>` : '');
            return `<tr>
                <td>${escapeHtml(t.name)}<div style="color:#666;font-size:11px;">${t.id}</div></td>
                <td>${escapeHtml(t.status)}${anNote}</td>
                <td>${t.file_count}</td>
                <td>${map}</td>
                <td>
                    <button type="button" onclick="startAiAnalyze('${t.id}')">AI分析</button>
                    <button type="button" onclick="openInsightPanel('${t.id}')">标签</button>
                    <button type="button" onclick="previewAnalysisTask('${t.id}')">预览</button>
                    <button type="button" onclick="executeAnalysisTask('${t.id}')">执行</button>
                    <button type="button" onclick="rollbackAnalysisTask('${t.id}')">回滚</button>
                    <button type="button" onclick="useTaskForEval('${t.id}')">评测</button>
                    <div class="analysis-note" style="margin-top:4px;">${evalText}</div>
                </td>
            </tr>`;
        }).join('');
    } catch (e) {
        rows.innerHTML = `<tr><td colspan="5" style="color:#ff8a8a;">${escapeHtml(e.message || String(e))}</td></tr>`;
    }
}

function copyText(s) {
    if (!s) return;
    navigator.clipboard?.writeText(s);
}

async function createAnalysisTask() {
    const workIds = Array.from(selectedIds);
    const name = (document.getElementById('taskNameInput').value || '').trim();
    try {
        const res = await fetch('/api/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({ name, work_ids: workIds }),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '创建失败');
        alert(`任务已创建：${data.task.name}（视频 ${data.task.file_count} 个）`);
        await loadTaskList();
    } catch (e) {
        alert('创建任务失败：' + (e.message || String(e)));
    }
}

async function previewAnalysisTask(id) {
    try {
        const res = await fetch('/api/tasks/' + encodeURIComponent(id) + '/preview', { method: 'POST' });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '预览失败');
        const top = (data.preview || []).slice(0, 25);
        const text = top.map((x, i) => `${String(i + 1).padStart(3, '0')}. ${x.src} -> ${x.dst_name}`).join('\n');
        document.getElementById('analysisPreview').textContent =
            `任务 ${id} 预览 ${data.preview.length} 条（展示前 25 条）\n` + text;
        await loadTaskList();
    } catch (e) {
        alert('预览失败：' + (e.message || String(e)));
    }
}

async function executeAnalysisTask(id) {
    if (!confirm('确认执行批量重命名？\n规则：仅同目录改名，禁止跨目录移动。')) return;
    try {
        const res = await fetch('/api/tasks/' + encodeURIComponent(id) + '/execute', { method: 'POST' });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '执行失败');
        alert(`执行完成：成功 ${data.renamed}，失败 ${data.errors.length}\n映射文件：${data.mapping_file}`);
        await loadTaskList();
    } catch (e) {
        alert('执行失败：' + (e.message || String(e)));
    }
}

async function rollbackAnalysisTask(id) {
    if (!confirm('确认回滚该任务已执行的重命名？')) return;
    try {
        const res = await fetch('/api/tasks/' + encodeURIComponent(id) + '/rollback', { method: 'POST' });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '回滚失败');
        alert(`回滚完成：恢复 ${data.reverted}，失败 ${data.errors.length}`);
        await loadTaskList();
    } catch (e) {
        alert('回滚失败：' + (e.message || String(e)));
    }
}

async function saveTaskEvaluation() {
    const taskId = (document.getElementById('evalTaskSelect').value || '').trim();
    if (!taskId) { alert('请先选择任务'); return; }
    const approved = Number(document.getElementById('evalApprovedInput').value || 0);
    const reviewed = Number(document.getElementById('evalReviewedInput').value || 0);
    const topkHit = Number(document.getElementById('evalTopkHitInput').value || 0);
    const topkTotal = Number(document.getElementById('evalTopkTotalInput').value || 0);
    try {
        const res = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/evaluation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({
                approved: approved,
                reviewed: reviewed,
                topk_hit: topkHit,
                topk_total: topkTotal,
            }),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '保存评测失败');
        const e = data.eval || {};
        const text = `任务 ${taskId} 评测已保存\n` +
            `人工通过率: ${e.human_pass_rate || 0}% (${e.approved || 0}/${e.reviewed || 0})\n` +
            `Top-K 命中率: ${e.topk_hit_rate || 0}% (${e.topk_hit || 0}/${e.topk_total || 0})`;
        document.getElementById('evalResult').textContent = text;
        await loadTaskList();
    } catch (err) {
        alert('保存评测失败：' + (err.message || String(err)));
    }
}

function useTaskForEval(taskId) {
    const sel = document.getElementById('evalTaskSelect');
    if (!sel) return;
    sel.value = taskId;
    sel.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function parseTagsInput(s) {
    if (!s || !String(s).trim()) return [];
    return String(s).split(/[,，、;；|]+/).map(x => x.trim()).filter(Boolean);
}

function composeTagsToSentence(tags) {
    if (!tags || !tags.length) return '';
    const parts = tags.map((t) => String(t).trim()).filter(Boolean).slice(0, 12);
    if (parts.length === 0) return '';
    if (parts.length === 1) return parts[0] + '。';
    return parts.join('、') + '。';
}

function stopInsightPoll() {
    if (insightPollTimer) {
        clearInterval(insightPollTimer);
        insightPollTimer = null;
    }
}

async function openInsightPanel(id) {
    switchTab('analysis');
    stopInsightPoll();
    insightTaskId = id;
    const el = document.getElementById('insightTaskLabel');
    if (el) el.textContent = '当前任务：' + id;
    await loadTaskDetail(false);
}

async function startAiAnalyze(id) {
    switchTab('analysis');
    try {
        const res = await fetch('/api/tasks/' + encodeURIComponent(id) + '/analyze', {
            method: 'POST',
            cache: 'no-store',
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '启动失败');
        await openInsightPanel(id);
        await loadTaskList();
    } catch (e) {
        alert('AI 分析：' + (e.message || String(e)));
    }
}

async function loadTaskDetail(silentPoll) {
    const progressEl = document.getElementById('analyzeProgress');
    if (!insightTaskId) {
        if (progressEl) progressEl.style.display = 'none';
        return null;
    }
    try {
        const r = await fetch('/api/tasks/' + encodeURIComponent(insightTaskId), { cache: 'no-store' });
        const data = await r.json();
        if (!data.ok) throw new Error(data.error || '加载失败');
        const t = data.task;
        const aj = t.analyze_job || {};
        if (progressEl) {
            if (aj.state === 'running') {
                progressEl.style.display = '';
                const cur = aj.current_path ? escapeHtml(aj.current_path) : '';
                const phase = escapeHtml(aj.phase_detail || aj.phase || '');
                const om = aj.model ? escapeHtml(String(aj.model)) : '';
                const oh = aj.ollama_host ? escapeHtml(String(aj.ollama_host)) : '';
                progressEl.innerHTML =
                    `<div><b>进度 ${Number(aj.done || 0)}/${Number(aj.total || 0)}</b> · ${phase}</div>` +
                    (oh || om ? `<div style="font-size:11px;color:#9cf;margin-top:4px;">Ollama ${oh}${om ? ' · 模型 ' + om : ''}</div>` : '') +
                    (cur ? `<div style="font-size:11px;word-break:break-all;margin-top:4px;color:#ccc;">当前：${cur}</div>` : '');
            } else {
                progressEl.style.display = (aj.total > 0 || aj.phase_detail) ? '' : 'none';
                if (aj.state === 'done' && aj.total) {
                    progressEl.textContent = `分析阶段结束（共 ${aj.total} 个视频）`;
                } else if (aj.error) {
                    progressEl.innerHTML = `<span style="color:#f88;">${escapeHtml(String(aj.error))}</span>`;
                } else {
                    progressEl.textContent = '';
                }
            }
        }
        renderInsightRows(t);
        if (!silentPoll && aj.state === 'running') {
            stopInsightPoll();
            insightPollTimer = setInterval(() => { loadTaskDetail(true); }, 1000);
        }
        if (silentPoll && aj.state !== 'running') {
            stopInsightPoll();
            loadTaskList();
        }
        return aj.state;
    } catch (e) {
        if (progressEl && !silentPoll) {
            progressEl.style.display = '';
            progressEl.innerHTML = `<span style="color:#f88;">加载任务详情失败：${escapeHtml(e.message || String(e))}</span>`;
        }
        if (!silentPoll) alert(e.message || String(e));
        return null;
    }
}

function renderInsightRows(t) {
    const wrap = document.getElementById('insightTableWrap');
    if (!wrap) return;
    const aj = t.analyze_job || {};
    const jobRunning = aj.state === 'running';
    const rows = (t.insight_rows && t.insight_rows.length)
        ? t.insight_rows.slice()
        : (t.source_files || []).map((p) => Object.assign({ path: p }, (t.insights || {})[p] || {}));
    if (rows.length === 0) {
        wrap.innerHTML = '<div class="analysis-note">该任务没有视频文件。</div>';
        return;
    }
    let html = '<table class="analysis-table"><thead><tr><th>画面预览</th><th>文件</th><th>时间（可改）</th><th>地点</th><th>事件</th><th>标签（可改，供核对）</th><th>短语（可改）</th><th>状态</th><th>准确</th></tr></thead><tbody>';
    rows.forEach((ins) => {
        const p = ins.path || '';
        const st = ins.llm_status || 'idle';
        let statusText = '未分析';
        if (st === 'done') statusText = '已分析';
        else if (st === 'running') {
            if (aj.phase === 'ollama') statusText = '分析中（Ollama）…';
            else if (aj.phase === 'extract_frames') statusText = '分析中（抽帧）…';
            else statusText = '分析中…';
        } else if (st === 'error') statusText = '失败';
        else if (jobRunning && st === 'idle') statusText = '排队中';
        const errLine = (st === 'error' && ins.error) ? `<div style="color:#f88;font-size:10px;">${escapeHtml(String(ins.error).slice(0, 220))}</div>` : '';
        const tagsDisp = (ins.confirmed_tags && ins.confirmed_tags.length) ? ins.confirmed_tags : (ins.tags || []);
        const tagsStr = tagsDisp.join('、');
        const phraseVal = (ins.confirmed_phrase || ins.phrase || '');
        const autoFromTags = composeTagsToSentence(tagsDisp);
        const dis = (st !== 'done') ? 'disabled' : '';
        const checked = ins.user_confirmed ? 'checked' : '';
        const shortName = String(p).split(/[\\/]+/).pop() || p;
        const playUrl = buildVideoPlayUrl(p);
        const thumbSrc = '/api/preview-thumb?path=' + encodeURIComponent(p);
        html += `<tr data-path="${encodeURIComponent(p)}">
            <td style="width:168px;vertical-align:top;padding:6px;">
                <a href="${playUrl}" target="_blank" rel="noopener noreferrer" title="新标签页播放原片" style="display:inline-block;line-height:0;border-radius:6px;overflow:hidden;border:1px solid #333;">
                    <img src="${thumbSrc}" alt="" width="160" height="90" loading="lazy" style="object-fit:cover;display:block;background:#1a1a1a;" onerror="window.insightRowThumbError(event)">
                </a>
                <div style="margin-top:4px;font-size:10px;"><a href="${playUrl}" target="_blank" rel="noopener noreferrer">▶ 播放核对</a></div>
            </td>
            <td style="max-width:140px;font-size:11px;word-break:break-all;" title="${escapeHtml(p)}">${escapeHtml(shortName)}</td>
            <td><input type="text" class="insight-time-inp" value="${escapeHtml(ins.confirmed_time || ins.time_guess || '')}" style="width:130px;font-size:11px;" ${st === 'done' || st === 'error' ? '' : 'disabled'} placeholder="YYYY-MM-DD HH:MM:SS"></td>
            <td style="font-size:11px;">${escapeHtml(ins.place_guess || '')}</td>
            <td style="font-size:11px;">${escapeHtml(ins.event_guess || '')}</td>
            <td><input type="text" class="insight-tags-inp" value="${escapeHtml(tagsStr)}" style="width:120px;font-size:11px;" ${dis}></td>
            <td><textarea class="insight-phrase-ta" rows="2" style="width:160px;font-size:11px;" ${dis}>${escapeHtml(phraseVal)}</textarea>
                <div class="insight-auto-from-tags" style="font-size:10px;color:#8ab4f8;margin-top:4px;line-height:1.35;">由标签连成句：<span class="insight-auto-phrase-text">${escapeHtml(autoFromTags)}</span></div></td>
            <td style="font-size:11px;">${statusText}${errLine}</td>
            <td style="text-align:center;"><input type="checkbox" class="insight-confirm-cb" ${checked} ${dis} title="确认标签与短语无误"></td>
        </tr>`;
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;
    bindInsightTableLiveAutoPhrase();
}

function bindInsightTableLiveAutoPhrase() {
    const wrap = document.getElementById('insightTableWrap');
    if (!wrap || wrap._insightAutoPhraseBound) return;
    wrap._insightAutoPhraseBound = true;
    wrap.addEventListener('input', (e) => {
        const inp = e.target && e.target.closest && e.target.closest('.insight-tags-inp');
        if (!inp || !wrap.contains(inp)) return;
        const tr = inp.closest('tr');
        const span = tr && tr.querySelector('.insight-auto-phrase-text');
        if (span) span.textContent = composeTagsToSentence(parseTagsInput(inp.value));
    });
}

async function saveInsightConfirms() {
    if (!insightTaskId) { alert('请先点某一行的「标签」打开任务'); return; }
    const wrap = document.getElementById('insightTableWrap');
    if (!wrap) return;
    const trs = wrap.querySelectorAll('tbody tr[data-path]');
    const confirms = [];
    trs.forEach(tr => {
        const path = decodeURIComponent(tr.getAttribute('data-path') || '');
        const ta = tr.querySelector('.insight-phrase-ta');
        const ph = ta ? ta.value : '';
        const tin = tr.querySelector('.insight-tags-inp');
        const tags = tin ? parseTagsInput(tin.value) : [];
        const timeInp = tr.querySelector('.insight-time-inp');
        const tstr = timeInp ? timeInp.value : '';
        const cb = tr.querySelector('.insight-confirm-cb');
        const confirmed = cb && cb.checked;
        confirms.push({ path: path, phrase: ph, tags: tags, time: tstr, confirmed: confirmed });
    });
    try {
        const res = await fetch('/api/tasks/' + encodeURIComponent(insightTaskId) + '/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({ confirms: confirms }),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '保存失败');
        await loadTaskDetail(false);
        await loadTaskList();
        alert('已保存确认状态（请对「已分析」的条目勾选「准确」才会解锁预览重命名）。');
    } catch (e) {
        alert('保存失败：' + (e.message || String(e)));
    }
}

// 搜索防抖
let searchTimer = null;
function debouncedUpdate() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        localStorage.setItem('mb_search', searchInput.value);
        sortAndRenderAll();
    }, 200);
}
searchInput.addEventListener('input', debouncedUpdate);

// 排序
function sortWorks(list) {
    const copy = [...list];
    switch (sortKey) {
        case 'name': copy.sort((a,b) => a.name.localeCompare(b.name)); break;
        case 'files': copy.sort((a,b) => (b.video_count+b.image_count) - (a.video_count+a.image_count)); break;
        case 'size': copy.sort((a,b) => b.total_size - a.total_size); break;
        case 'duration': copy.sort((a,b) => b.total_duration - a.total_duration); break;
        case 'mtime': copy.sort((a,b) => b.mtime - a.mtime); break;
    }
    return copy;
}
sortSelect.addEventListener('change', () => {
    sortKey = sortSelect.value;
    localStorage.setItem('mb_sort', sortKey);
    sortAndRenderAll();
});

document.querySelectorAll('.filters button').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        filterType = btn.dataset.filter;
        localStorage.setItem('mb_filter', filterType);
        sortAndRenderAll();
    });
});

// 批量选择
function updateBatchBar() {
    batchCountEl.textContent = selectedIds.size;
    const show = selectedIds.size > 0;
    batchBar.classList.toggle('show', show);
    document.body.classList.toggle('batch-open', show);
}
function toggleSelect(workId, el) {
    if (selectedIds.has(workId)) { selectedIds.delete(workId); el.checked = false; }
    else { selectedIds.add(workId); el.checked = true; }
    updateBatchBar();
}
function selectAllVisible() {
    const q = searchInput.value.trim().toLowerCase();
    const sorted = sortWorks(allWorks);
    for (const w of sorted) {
        if (workMatchesFilter(w, filterType) && workMatchesSearch(w, q)) {
            selectedIds.add(w.id);
        }
    }
    document.querySelectorAll('.work-card .chk').forEach(c => c.checked = true);
    updateBatchBar();
}
function clearSelection() {
    selectedIds.clear();
    document.querySelectorAll('.work-card .chk').forEach(c => c.checked = false);
    updateBatchBar();
}
function batchMarkReview(state) {
    const list = Array.from(selectedIds);
    if (list.length === 0) return;
    let o = {};
    try { o = JSON.parse(localStorage.getItem('mb_review_tags') || '{}'); } catch (e) {}
    for (const id of list) { o[id] = state; }
    localStorage.setItem('mb_review_tags', JSON.stringify(o));
    clearSelection();
    sortAndRenderAll();
}
async function batchOpenFinder() {
    const list = Array.from(selectedIds);
    for (const id of list) {
        const w = allWorks.find(x => x.id === id);
        if (w) {
            fetch('/open?path=' + encodeURIComponent(w.path));
            await new Promise(r => setTimeout(r, 300));
        }
    }
}

function workMatchesFilter(w, ft) {
    if (ft === 'all') return true;
    if (ft === 'video') return w.video_count > 0;
    if (ft === 'image') return w.image_count > 0;
    if (ft === 'pending') return getWorkReviewTag(w.id) !== 'kept';
    return true;
}
function workMatchesSearch(w, q) {
    if (!q) return true;
    const lq = q.toLowerCase();
    if (w.name.toLowerCase().includes(lq)) return true;
    return w.items.some(it => it.name.toLowerCase().includes(lq));
}

function mbEnsureThumbIO() {
    if (mbThumbIO) return;
    mbThumbIO = new IntersectionObserver(function (ents) {
        for (let k = 0; k < ents.length; k++) {
            const en = ents[k];
            if (!en.isIntersecting) continue;
            const im = en.target;
            const ds = im.getAttribute('data-src');
            if (ds) {
                im.src = ds;
                im.removeAttribute('data-src');
            }
            im.classList.remove('mb-lazy');
            mbThumbIO.unobserve(im);
        }
    }, { root: null, rootMargin: '140px 0px 160px 0px', threshold: 0.01 });
}
function mbRefreshLazyThumbs(root) {
    mbEnsureThumbIO();
    if (!mbThumbIO || !root) return;
    root.querySelectorAll('img.mb-lazy').forEach(function (im) {
        mbThumbIO.observe(im);
    });
}

function renderVirtualSlice(start) {
    const list = mbVirtual.list;
    if (!list.length) return;
    const vh = window.innerHeight || 800;
    const slice = Math.min(list.length, Math.ceil(vh / MB_V_ROW) + 16);
    let s = Math.max(0, Math.min(start, Math.max(0, list.length - slice)));
    const end = Math.min(list.length, s + slice);
    mbVirtual.anchor = s;
    container.innerHTML = '';
    displayedIds.clear();
    const top = document.createElement('div');
    top.className = 'mb-v-spacer';
    top.style.height = (s * MB_V_ROW) + 'px';
    top.setAttribute('aria-hidden', 'true');
    container.appendChild(top);
    for (let i = s; i < end; i++) {
        const w = list[i];
        displayedIds.add(w.id);
        container.appendChild(createWorkCard(w, i));
    }
    const bot = document.createElement('div');
    bot.className = 'mb-v-spacer';
    bot.style.height = ((list.length - end) * MB_V_ROW) + 'px';
    bot.setAttribute('aria-hidden', 'true');
    container.appendChild(bot);
    mbRefreshLazyThumbs(container);
}

function mbScheduleVirtualSync() {
    if (!mbVirtual.active) return;
    if (mbVirtScrollRaf) cancelAnimationFrame(mbVirtScrollRaf);
    mbVirtScrollRaf = requestAnimationFrame(function () {
        mbVirtScrollRaf = 0;
        const list = mbVirtual.list;
        if (!list.length) return;
        const y = window.scrollY;
        const approx = Math.floor(Math.max(0, y - 120) / MB_V_ROW);
        const target = Math.max(0, approx - 6);
        if (Math.abs(target - mbVirtual.anchor) > 5) renderVirtualSlice(target);
    });
}

function hideCardCtx() {
    if (!cardCtxMenu) return;
    cardCtxMenu.style.display = 'none';
    cardCtxMenu.setAttribute('aria-hidden', 'true');
    mbCtxWork = null;
}
function showCardCtx(clientX, clientY, work) {
    if (!cardCtxMenu || !work) return;
    mbCtxWork = work;
    cardCtxMenu.style.display = 'block';
    cardCtxMenu.setAttribute('aria-hidden', 'false');
    const pad = 8;
    const w = cardCtxMenu.offsetWidth || 200;
    const h = cardCtxMenu.offsetHeight || 160;
    let x = clientX;
    let y = clientY;
    if (x + w + pad > window.innerWidth) x = window.innerWidth - w - pad;
    if (y + h + pad > window.innerHeight) y = window.innerHeight - h - pad;
    cardCtxMenu.style.left = Math.max(pad, x) + 'px';
    cardCtxMenu.style.top = Math.max(pad, y) + 'px';
}

function attachCardLongPress(div, work) {
    let t = null;
    let sx = 0;
    let sy = 0;
    function cancel() {
        if (t) { clearTimeout(t); t = null; }
    }
    div.addEventListener('pointerdown', function (e) {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        if (e.target.closest('.chk') || e.target.closest('button') || e.target.closest('a')) return;
        sx = e.clientX;
        sy = e.clientY;
        cancel();
        t = setTimeout(function () {
            t = null;
            showCardCtx(sx, sy, work);
        }, 520);
    });
    div.addEventListener('pointermove', function (e) {
        if (!t) return;
        if (Math.abs(e.clientX - sx) > 14 || Math.abs(e.clientY - sy) > 14) cancel();
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) {
        div.addEventListener(ev, cancel);
    });
}

function mbNormPath(p) {
    return (p || '').replace(/\\/g, '/').replace(/\/+$/, '');
}

function workCanRemoveEmptyFolder(work) {
    const inp = document.getElementById('scanRootInput');
    const root = inp ? inp.value.trim() : '';
    if (!root || !work || !work.path) return false;
    return mbNormPath(work.path) !== mbNormPath(root);
}

function deleteCurrentWork() {
    const work = allWorks.find(function (w) { return w.id === galleryState.workId; });
    if (work) deleteWorkAllFiles(work);
}

async function deleteWorkAllFiles(work) {
    if (!work || !work.items.length) return;
    const folderNote = workCanRemoveEmptyFolder(work) ? '；若文件夹已空将一并移除' : '';
    if (!confirm('将永久删除「' + work.name + '」内全部 ' + work.items.length + ' 个媒体文件' + folderNote + '，不可恢复。确定？')) return;
    const paths = work.items.map(function (it) { return it.path; });
    try {
        const res = await fetch('/api/works/delete-all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({ work_path: work.path, paths: paths }),
        });
        const data = await res.json();
        if (!data.ok) {
            alert('删除失败: ' + (data.error || '未知错误'));
            return;
        }
        const deletedSet = new Set(data.deleted_paths || []);
        for (let i = work.items.length - 1; i >= 0; i--) {
            if (deletedSet.has(work.items[i].path)) {
                const it = work.items[i];
                if (it.type === 'video') work.video_count--;
                else work.image_count--;
                work.items.splice(i, 1);
            }
        }
        hideCardCtx();
        const modal = document.getElementById('modal');
        const inGallery = modal && modal.classList.contains('active') && galleryState.workId === work.id;
        if (work.items.length === 0) {
            const idx = allWorks.findIndex(function (w) { return w.id === work.id; });
            if (idx >= 0) allWorks.splice(idx, 1);
            closeModal();
            sortAndRenderAll();
        } else {
            sortAndRenderAll();
            if (data.errors && data.errors.length) {
                alert('部分文件未能删除（仍剩余 ' + work.items.length + ' 个）。失败项已记入废纸篓，可在页眉「废纸篓」批量重试。');
            }
            if (inGallery) {
                if (galleryState.itemIdx >= work.items.length) {
                    galleryState.itemIdx = Math.max(0, work.items.length - 1);
                }
                renderGallery();
            }
        }
        mbRefreshTrashBadge(true);
    } catch (err) {
        alert('请求失败: ' + (err.message || String(err)));
    }
}

function preloadAdjacentWorks() {
    const nav = galleryWorkNavListAndIndex();
    const list = nav.list;
    const wi = nav.wi;
    if (!list.length || wi < 0) return;
    function preloadOne(w) {
        if (!w || !w.items || !w.items.length) return;
        const it = w.items[0];
        let u = '';
        if (it.type === 'video') {
            const tl = it.thumbs || [];
            u = tl.length ? buildThumbUrl(tl[0]) : (it.thumb ? buildThumbUrl(it.thumb) : '');
        } else {
            u = it.thumb ? buildThumbUrl(it.thumb) : '';
        }
        if (!u) return;
        const im = new Image();
        im.src = u;
    }
    if (wi > 0) preloadOne(list[wi - 1]);
    if (wi < list.length - 1) preloadOne(list[wi + 1]);
}

function setupGalleryVideoPinch() {
    const wrap = document.getElementById('galleryVideoWrap');
    const vid = document.getElementById('galleryVideo');
    if (!wrap || !vid) return;
    let scale = 1;
    let startDist = 0;
    function dist(a, b) {
        const dx = a.clientX - b.clientX;
        const dy = a.clientY - b.clientY;
        return Math.sqrt(dx * dx + dy * dy);
    }
    wrap.addEventListener('touchstart', function (e) {
        if (e.touches.length === 2) startDist = dist(e.touches[0], e.touches[1]);
    }, { passive: true });
    wrap.addEventListener('touchmove', function (e) {
        if (e.touches.length !== 2 || !startDist) return;
        const d = dist(e.touches[0], e.touches[1]);
        const ratio = d / startDist;
        startDist = d;
        scale = Math.min(4, Math.max(1, scale * ratio));
        wrap.style.transform = 'scale(' + scale + ')';
        wrap.style.transformOrigin = 'center center';
        e.preventDefault();
    }, { passive: false });
    wrap.addEventListener('touchend', function (e) {
        if (e.touches.length < 2) startDist = 0;
        if (e.touches.length === 0 && scale <= 1.02) {
            scale = 1;
            wrap.style.transform = '';
        }
    }, { passive: true });
}

function createWorkCard(work, virtualIndex) {
    const div = document.createElement('div');
    div.className = 'work-card';
    div.dataset.id = work.id;
    if (virtualIndex !== undefined && virtualIndex !== null) div.dataset.vi = String(virtualIndex);

    const chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.className = 'chk';
    chk.checked = selectedIds.has(work.id);
    chk.onclick = (e) => { e.stopPropagation(); toggleSelect(work.id, chk); };
    div.appendChild(chk);

    let contentHtml = '<div class="work-content">';
    for (let i = 0; i < work.items.length; i++) {
        const it = work.items[i];
        if (it.type === 'video') {
            const dur = it.duration ? fmtDuration(it.duration) : '';
            contentHtml += `<div class="video-row">`;
            contentHtml += `<div class="video-info">
                <div class="vname">${escapeHtml(it.name)}</div>
                <div class="vdur">▶ ${dur} · ${fmtSize(it.size)}</div>
            </div>`;
            contentHtml += `<div class="thumb-strip">`;
            const tlist = it.thumbs || [];
            for (let j = 0; j < tlist.length; j++) {
                const url = buildThumbUrl(tlist[j]);
                const isPh = url === '';
                const lazyAttr = url && !isPh
                    ? ` class="mb-lazy" data-src="${url.replace(/"/g, '&quot;')}" src="${MB_LAZY_PLACEHOLDER}"`
                    : ` src="${url}"`;
                contentHtml += `<div class="thumb" data-item-idx="${i}" data-thumb-idx="${j}">
                    <img${lazyAttr} alt="" draggable="false" onload="this.classList.add('loaded')" onerror="this.classList.add('loaded'); this.style.opacity='0.3';">
                    <div class="loader"></div>
                    ${isPh ? '<div class="ph-icon">❓</div>' : ''}
                </div>`;
            }
            contentHtml += `</div></div>`;
        } else {
            const url = buildThumbUrl(it.thumb);
            const lazyAttr = url
                ? ` class="mb-lazy" data-src="${url.replace(/"/g, '&quot;')}" src="${MB_LAZY_PLACEHOLDER}"`
                : ` src=""`;
            contentHtml += `<div class="image-row" data-item-idx="${i}">
                <img${lazyAttr} alt="" draggable="false" onload="this.classList.add('loaded')" onerror="this.classList.add('loaded'); this.style.opacity='0.3';">
            </div>`;
        }
    }
    contentHtml += '</div>';

    const title = escapeHtml(work.name);
    const durText = work.main_duration ? `⏱ ${fmtDuration(work.main_duration)} · ` : '';
    const meta = `${durText}${work.video_count}视频 · ${work.image_count}图片 · ${fmtSize(work.total_size)}`;

    const info = document.createElement('div');
    info.className = 'info';
    const reviewTop = buildReviewStripHtml(work.id);
    info.innerHTML = reviewTop + `<div class="title-row">
        <div class="title" title="${title}" onclick="event.stopPropagation(); openGallery(${JSON.stringify(work.id)}, 0, -1)">${title}</div>
    </div>
    <div class="meta">${meta}</div>`;

    const wrap = document.createElement('div');
    wrap.innerHTML = contentHtml;
    while (wrap.firstChild) div.appendChild(wrap.firstChild);
    div.appendChild(info);

    div.querySelectorAll('.thumb-strip .thumb').forEach((el) => {
        el.addEventListener('click', (e) => {
            e.stopPropagation();
            const ii = parseInt(el.getAttribute('data-item-idx'), 10);
            const jj = parseInt(el.getAttribute('data-thumb-idx'), 10);
            if (Number.isNaN(ii) || Number.isNaN(jj)) return;
            openGallery(work.id, ii, jj);
        });
    });
    div.querySelectorAll('.image-row').forEach((el) => {
        el.addEventListener('click', (e) => {
            e.stopPropagation();
            const ii = parseInt(el.getAttribute('data-item-idx'), 10);
            if (Number.isNaN(ii)) return;
            openGallery(work.id, ii, -1);
        });
    });

    div.addEventListener('click', (e) => {
        if (e.target.closest('.chk') || e.target.closest('.title') || e.target.closest('.thumb') || e.target.closest('.image-row') || e.target.closest('.review-strip')) return;
        openGallery(work.id, 0, -1);
    });

    attachCardLongPress(div, work);
    return div;
}

function getFilteredSortedWorks() {
    const q = searchInput.value.trim().toLowerCase();
    let list = allWorks.filter(w => workMatchesFilter(w, filterType) && workMatchesSearch(w, q));
    return sortWorks(list);
}

function refreshEmptyHint(filteredCount) {
    const el = document.getElementById('emptyHint');
    if (!el) return;
    if (lastEnumError) {
        el.style.display = 'block';
        el.className = 'empty-hint empty-error';
        el.innerHTML = '<h2>无法枚举扫描目录</h2><p>' + escapeHtml(lastEnumError) + '</p>'
            + '<ul><li>检查 <code>MB_ROOT_DIR</code> 路径是否存在、外置盘是否已挂载</li>'
            + '<li>关闭后使用 <code>MB_ROOT_DIR=/正确路径</code> 再启动</li></ul>';
        return;
    }
    if (!scanPollDone) {
        el.style.display = 'none';
        el.innerHTML = '';
        return;
    }
    if (allWorks.length === 0) {
        el.style.display = 'block';
        el.className = 'empty-hint';
        el.innerHTML = '<h2>未发现任何作品</h2><p>本工具将<strong>扫描根目录下的一级子文件夹</strong>（每个含视频/图片的文件夹算一个作品）。</p>'
            + '<ul><li>若媒体都在子文件夹里：确认它们直接在 <code>MB_ROOT_DIR</code> 下面（不是更深层才放文件夹）</li>'
            + '<li>若视频/图片<strong>直接平铺在根目录</strong>：会显示为名为「根目录内的媒体…」的一个作品（本次已支持）</li>'
            + '<li>扩展名需在支持列表内（如 mp4、jpg 等）</li></ul>'
            + '<p style="margin-top:10px;color:#777;font-size:12px;">在页眉「扫描根目录」输入路径后点「应用并扫描」；仍可用环境变量 <code>MB_ROOT_DIR</code> 作为启动默认。</p>';
        return;
    }
    if (filteredCount === 0) {
        el.style.display = 'block';
        el.className = 'empty-hint';
        el.innerHTML = '<h2>当前筛选/搜索下没有结果</h2><p>库中仍有 ' + allWorks.length + ' 个作品，可能被搜索词或「有视频 / 有图片」筛掉了。</p>'
            + '<div class="cta-row"><button type="button" onclick="resetFilters()">恢复全部显示</button>'
            + '<button type="button" class="secondary" onclick="localStorage.clear(); location.reload();">清除本地记录并刷新</button></div>';
        return;
    }
    el.style.display = 'none';
    el.innerHTML = '';
}

function resetFilters() {
    searchInput.value = '';
    filterType = 'all';
    sortKey = 'name';
    sortSelect.value = 'name';
    document.querySelectorAll('.filters button').forEach(b => {
        b.classList.toggle('active', b.dataset.filter === 'all');
    });
    localStorage.removeItem('mb_search');
    localStorage.setItem('mb_filter', 'all');
    localStorage.setItem('mb_sort', 'name');
    sortAndRenderAll();
}

function sortAndRenderAll() {
    const list = getFilteredSortedWorks();
    container.innerHTML = '';
    displayedIds.clear();
    renderOffset = 0;
    mbVirtual.active = false;
    if (scanPollDone || allWorks.length > 0) {
        if (allWorks.length > 0) {
            const narrowed = list.length !== allWorks.length || searchInput.value.trim() !== '' || filterType !== 'all';
            statusEl.textContent = narrowed
                ? ('显示 ' + list.length + ' / 共 ' + allWorks.length + ' 个作品')
                : ('共 ' + list.length + ' 个作品');
        } else {
            statusEl.textContent = '共 0 个作品';
        }
    }
    refreshEmptyHint(list.length);
    if (list.length > MB_V_THRESHOLD) {
        mbVirtual.active = true;
        mbVirtual.list = list;
        mbVirtual.anchor = 0;
        renderVirtualSlice(0);
    } else {
        renderMoreWorks(list);
    }
}

function renderMoreWorks(list) {
    if (mbVirtual.active) return;
    const slice = list.slice(renderOffset, renderOffset + BATCH_STEP);
    for (const w of slice) {
        if (displayedIds.has(w.id)) continue;
        displayedIds.add(w.id);
        container.appendChild(createWorkCard(w));
    }
    renderOffset += slice.length;
    mbRefreshLazyThumbs(container);
}

/** 视口内作品卡片索引范围（与 sticky 页眉大致对齐的 topBar） */
function getVisibleWorkCardRange() {
    const cards = Array.from(container.querySelectorAll('.work-card'));
    if (!cards.length) return { cards, first: -1, last: -1, vh: window.innerHeight, topBar: 88 };
    const vh = window.innerHeight;
    const topBar = 88;
    let first = -1;
    let last = -1;
    for (let i = 0; i < cards.length; i++) {
        const r = cards[i].getBoundingClientRect();
        if (r.bottom > topBar && r.top < vh) {
            if (first < 0) first = i;
            last = i;
        }
    }
    return { cards, first, last, vh, topBar };
}

/**
 * 审阅列表：滚到「下一个作品（文件夹）」卡片。
 * 以视口内最后一个可见卡片为锚，滚到下一张；必要时懒加载。
 */
function goToNextWorkFolder() {
    const list = getFilteredSortedWorks();
    if (mbVirtual.active) {
        const cards = Array.from(container.querySelectorAll('.work-card'));
        if (!cards.length) return;
        const vh = window.innerHeight;
        const topBar = 88;
        let bestVi = -1;
        for (let i = 0; i < cards.length; i++) {
            const r = cards[i].getBoundingClientRect();
            if (r.bottom <= topBar || r.top >= vh) continue;
            const vi = parseInt(cards[i].dataset.vi, 10);
            if (!Number.isNaN(vi)) bestVi = Math.max(bestVi, vi);
        }
        if (bestVi < 0) {
            window.scrollBy({ top: Math.max(160, vh * 0.85), behavior: 'smooth' });
            return;
        }
        const nextVi = bestVi + 1;
        if (nextVi < list.length) {
            window.scrollTo({ top: nextVi * MB_V_ROW - 80, behavior: 'smooth' });
            setTimeout(mbScheduleVirtualSync, 350);
        }
        return;
    }
    let { cards, first, last, vh } = getVisibleWorkCardRange();
    if (!cards.length) return;
    let nextIdx = last + 1;
    if (nextIdx >= cards.length && renderOffset < list.length) {
        renderMoreWorks(list);
        const r2 = getVisibleWorkCardRange();
        cards = r2.cards;
        last = r2.last;
        nextIdx = last + 1;
    }
    if (nextIdx >= 0 && nextIdx < cards.length && cards[nextIdx]) {
        cards[nextIdx].scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
    }
    window.scrollBy({ top: Math.max(160, vh * 0.85), behavior: 'smooth' });
}

/** 审阅列表：滚到「上一个作品」卡片（以视口内首张可见卡片为锚）。 */
function goToPrevWorkFolder() {
    const list = getFilteredSortedWorks();
    if (mbVirtual.active) {
        const cards = Array.from(container.querySelectorAll('.work-card'));
        if (!cards.length) return;
        const vh = window.innerHeight;
        const topBar = 88;
        let minVi = 999999999;
        for (let i = 0; i < cards.length; i++) {
            const r = cards[i].getBoundingClientRect();
            if (r.bottom <= topBar || r.top >= vh) continue;
            const vi = parseInt(cards[i].dataset.vi, 10);
            if (!Number.isNaN(vi)) minVi = Math.min(minVi, vi);
        }
        if (minVi === 999999999) {
            window.scrollBy({ top: -Math.max(160, vh * 0.85), behavior: 'smooth' });
            return;
        }
        const prevVi = minVi - 1;
        if (prevVi >= 0) {
            window.scrollTo({ top: prevVi * MB_V_ROW - 80, behavior: 'smooth' });
            setTimeout(mbScheduleVirtualSync, 350);
        }
        return;
    }
    const { cards, first, vh } = getVisibleWorkCardRange();
    if (!cards.length) return;
    const prevIdx = first - 1;
    if (prevIdx >= 0 && cards[prevIdx]) {
        cards[prevIdx].scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
    }
    window.scrollBy({ top: -Math.max(160, vh * 0.85), behavior: 'smooth' });
}

/** 审阅页且未打开画廊：⌘/Ctrl + ← → 切换作品文件夹（与画廊内左右键区分） */
function handleReviewListFolderNavKeys(e) {
    if (!(e.metaKey || e.ctrlKey)) return false;
    if (e.shiftKey || e.altKey) return false;
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return false;
    e.preventDefault();
    if (e.key === 'ArrowRight') goToNextWorkFolder();
    else goToPrevWorkFolder();
    return true;
}

let scrollTimer = null;
window.addEventListener('scroll', () => {
    if (window.scrollY > 400) backToTopBtn.classList.add('show');
    else backToTopBtn.classList.remove('show');
    if (mbVirtual.active) mbScheduleVirtualSync();
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
        if (mbVirtual.active) return;
        if ((window.innerHeight + window.scrollY) >= document.body.offsetHeight - 800) {
            const list = getFilteredSortedWorks();
            if (renderOffset < list.length) renderMoreWorks(list);
        }
    }, 100);
});

function scrollToTop() {
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function appendWorks(works) {
    let added = false;
    for (const w of works) {
        if (allWorks.find(x => x.id === w.id)) continue;
        allWorks.push(w);
        added = true;
    }
    if (added) sortAndRenderAll();
}

async function poll() {
    const myGen = pollGen;
    try {
        const res = await fetch('/api/progress?since=' + since);
        if (myGen !== pollGen) return;
        const data = await res.json();
        if (myGen !== pollGen) return;
        since = data.next_since;
        if (data.enum_error) lastEnumError = data.enum_error;
        if (data.scan_root) {
            const inp = document.getElementById('scanRootInput');
            if (inp && document.activeElement !== inp) inp.value = data.scan_root;
        }
        const pct = data.total > 0 ? (data.scanned / data.total * 100) : 0;
        progressFill.style.width = pct + '%';
        if (data.works && data.works.length > 0) appendWorks(data.works);
        if (data.done) {
            progressFill.style.width = '100%';
            progressFill.style.background = '#30d158';
            scanPollDone = true;
            if (allWorks.length === 0) {
                sortAndRenderAll();
            } else {
                const list = getFilteredSortedWorks();
                const narrowed = list.length !== allWorks.length || searchInput.value.trim() !== '' || filterType !== 'all';
                statusEl.textContent = narrowed
                    ? ('扫描完成，显示 ' + list.length + ' / 共 ' + allWorks.length + ' 个作品')
                    : ('扫描完成，共 ' + allWorks.length + ' 个作品');
                refreshEmptyHint(list.length);
            }
            mbRefreshTrashBadge(false);
        } else {
            if (allWorks.length === 0) {
                if (data.total > 0) {
                    statusEl.textContent = '扫描中… ' + data.scanned + '/' + data.total
                        + '（解析较慢的作品完成前，列表可能暂时为空）';
                } else {
                    statusEl.textContent = '未发现含媒体的子文件夹；若只有根目录平铺文件，完成后会出现「根目录内的媒体」作品';
                }
            } else {
                statusEl.textContent = '扫描中… ' + data.scanned + '/' + data.total;
            }
        }
    } catch (e) {
        if (myGen === pollGen) statusEl.textContent = '连接中断，重试中...';
    }
    if (!scanPollDone && myGen === pollGen) {
        setTimeout(poll, 1500);
    }
}

function openGallery(workId, itemIdx, thumbIdx) {
    const work = allWorks.find(w => w.id === workId);
    if (!work || !work.items.length) return;
    mbLastGalleryWorkId = workId;
    markWorkOpenedKept(workId);
    galleryState = {
        workId,
        itemIdx: Math.max(0, Math.min(itemIdx, work.items.length - 1)),
        thumbIdx: thumbIdx !== undefined ? thumbIdx : -1
    };
    sidebarOffset = 0;
    sidebarLimit = work.items.length > 80 ? 40 : work.items.length;
    renderGallery();
    document.getElementById('modal').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function renderGallery() {
    const work = allWorks.find(w => w.id === galleryState.workId);
    if (!work) return;
    const item = work.items[galleryState.itemIdx];
    if (!item) return;

    const mediaDiv = document.getElementById('modalMedia');
    const infoDiv = document.getElementById('modalFileInfo');
    const sidebarTitle = document.getElementById('sidebarTitle');
    const fileList = document.getElementById('fileList');
    const url = item.type === 'video' ? buildVideoPlayUrl(item.path) : buildFileUrl(item.path);

    if (item.type === 'video') {
        const needsTc = url.indexOf('/play?') >= 0;
        const overlayHtml = needsTc ? '<div id="transcodeOverlay" class="transcode-overlay">正在转码，请稍候…</div>' : '';
        mediaDiv.innerHTML = `<div class="video-wrap" id="galleryVideoWrap">${overlayHtml}<video id="galleryVideo" src="${url}" controls preload="metadata" playsinline></video></div>`;
        const v = document.getElementById('galleryVideo');
        const ov = document.getElementById('transcodeOverlay');
        if (needsTc && v && ov) {
            const hideOv = () => { ov.style.display = 'none'; };
            v.addEventListener('playing', hideOv, { once: true });
            v.addEventListener('canplay', hideOv, { once: true });
            v.addEventListener('error', () => {
                ov.textContent = '播放失败（请确认已安装 ffmpeg，或打包应用内包含 ffmpeg）';
                ov.style.display = 'flex';
            }, { once: true });
        }
        if (v) {
            v.onended = () => {
                const work2 = allWorks.find(w => w.id === galleryState.workId);
                if (work2 && galleryState.itemIdx < work2.items.length - 1) navigate(1);
            };
            v.addEventListener('loadedmetadata', () => {
                const ti = galleryState.thumbIdx;
                if (ti !== undefined && ti >= 0 && item.duration) {
                    v.currentTime = item.duration * (ti + 1) / (THUMB_COUNT + 1);
                } else {
                    v.currentTime = 0;
                }
                v.play().catch(() => {});
            }, { once: true });
            v.focus();
        }
        setupGalleryVideoPinch();
    } else {
        mediaDiv.innerHTML = `<div class="img-wrap" id="imgWrap"><img id="galleryImage" src="${url}" draggable="false" style="cursor:zoom-in;-webkit-user-drag:none;user-select:none;" /></div>`;
        setupImageZoom();
    }

    const dur = item.duration ? `⏱ ${fmtDuration(item.duration)} · ` : '';
    const res = (item.width && item.height) ? `${item.width}×${item.height} · ` : '';
    const codec = item.codec ? `${item.codec.toUpperCase()} · ` : '';
    const bitrate = item.bitrate ? `${(item.bitrate / 1000000).toFixed(1)}Mbps · ` : '';
    const fps = item.fps ? `${item.fps}fps · ` : '';
    const delBtn = `<span onclick="event.stopPropagation(); deleteCurrentItem();" style="cursor:pointer;color:#ff4444;margin-left:12px;font-size:12px;" title="删除当前：⌘I / Ctrl+I">🗑 删除</span>`;
    const delWorkBtn = `<span onclick="event.stopPropagation(); deleteCurrentWork();" style="cursor:pointer;color:#ff6666;margin-left:10px;font-size:12px;" title="删除本作品全部媒体：⌘⇧I / Ctrl+Shift+I">🗑 删本作品</span>`;
    const reviewBtn = `<button type="button" id="galleryReviewBtn" class="gallery-review-btn" data-work-id="${escapeHtml(work.id)}">标记</button>`;
    infoDiv.innerHTML = `${res}${codec}${bitrate}${fps}${dur}${escapeHtml(item.name)} · ${fmtSize(item.size)}${delBtn}${delWorkBtn}${reviewBtn}`;
    updateGalleryReviewBtn(work.id);
    const sh = document.getElementById('shortcutHint');
    if (sh) {
        if (item.type === 'video') {
            sh.textContent = '← → 快退/快进5s · Shift+←→ 翻文件（末档再→ 进下一作品）· ⌘←⌘→ 切作品 · J/L 退/进10s · 空格/K 播放 · F 全屏 · M 静音 · ↑↓ 音量 · ,/. 逐帧 · ESC 关闭 · ⌘I 删当前 · ⌘⇧I 删本作品';
        } else {
            sh.textContent = '← → 翻图片（末张再→ 进下一作品首张）· ⌘←⌘→ 切作品 · 滚轮缩放 · 拖拽平移 · 双击还原 · ESC 关闭 · ⌘I 删当前 · ⌘⇧I 删本作品';
        }
    }
    sidebarTitle.textContent = work.name;

    renderSidebar(fileList, work, item);

    const activeEl = fileList.querySelector('.file-item.active');
    if (activeEl) activeEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    preloadAdjacentWorks();
}

function renderSidebar(fileList, work, currentItem) {
    const dock = document.getElementById('sidebarThumbDock');
    let thumbsHtml = '';
    if (currentItem && currentItem.type === 'video' && currentItem.thumbs && currentItem.thumbs.length > 0) {
        thumbsHtml += '<div class="video-thumbs-panel"><h4>视频帧</h4><div class="vt-list">';
        for (let i = 0; i < currentItem.thumbs.length; i++) {
            const url = buildThumbUrl(currentItem.thumbs[i]);
            const active = i === galleryState.thumbIdx ? 'active' : '';
            const onclk = `event.stopPropagation(); jumpToThumb(${i})`;
            thumbsHtml += `<div class="vt-item ${active}" onclick="${onclk}">
                <img src="${url}" onload="this.classList.add('loaded')" onerror="this.style.display='none'">
            </div>`;
        }
        thumbsHtml += '</div></div>';
    }
    if (dock) dock.innerHTML = thumbsHtml;

    const total = work.items.length;
    const showLoadMore = total > sidebarLimit && sidebarOffset + sidebarLimit < total;
    const slice = work.items.slice(sidebarOffset, sidebarOffset + sidebarLimit);

    let html = '';
    for (let i = 0; i < slice.length; i++) {
        const globalIdx = sidebarOffset + i;
        const it = slice[i];
        const active = globalIdx === galleryState.itemIdx ? 'active' : '';
        const thumbUrl = it.thumb ? buildThumbUrl(it.thumb) : '';
        const dur2 = it.duration ? fmtDuration(it.duration) : (it.type === 'video' ? '' : '图片');
        const onclk = `jumpToItem(${globalIdx})`;
        html += `<div class="file-item ${active}" onclick="${onclk}">
            <div class="thumb-wrap">
                <img src="${thumbUrl}" onload="this.classList.add('loaded')" onerror="this.classList.add('loaded'); this.style.display='none'; this.nextElementSibling.style.display='flex';">
                <div class="ph">${it.type==='video'?'▶':'🖼'}</div>
            </div>
            <div class="file-meta">
                <div class="file-name">${escapeHtml(it.name)}</div>
                <div class="file-sub">${dur2} · ${fmtSize(it.size)}</div>
            </div>
        </div>`;
    }
    if (showLoadMore) {
        const remain = total - (sidebarOffset + sidebarLimit);
        html += `<div class="file-load-more" onclick="loadMoreSidebar()">加载更多 (${remain})</div>`;
    }
    fileList.innerHTML = html;
}

function loadMoreSidebar() {
    sidebarLimit += 40;
    const work = allWorks.find(w => w.id === galleryState.workId);
    if (work) {
        const fileList = document.getElementById('fileList');
        const item = work.items[galleryState.itemIdx];
        renderSidebar(fileList, work, item);
    }
}

function jumpToThumb(idx) {
    galleryState.thumbIdx = idx;
    renderGallery();
}

function jumpToItem(idx) {
    galleryState.itemIdx = idx;
    galleryState.thumbIdx = -1;
    renderGallery();
}

/**
 * 当前画廊在「全部作品」中的顺序（与审阅列表一致）；若当前作品被筛掉则退回全库排序。
 */
function galleryWorkNavListAndIndex() {
    const filtered = getFilteredSortedWorks();
    let wi = filtered.findIndex((w) => w.id === galleryState.workId);
    if (wi >= 0) return { list: filtered, wi };
    const sortedAll = sortWorks([...allWorks]);
    wi = sortedAll.findIndex((w) => w.id === galleryState.workId);
    return { list: sortedAll, wi };
}

/** 画廊内：切换到上一/下一作品；dir=+1 打开下一作品首个文件，dir=-1 打开上一作品最后一个文件 */
function openAdjacentWorkFromGallery(dir) {
    const { list, wi } = galleryWorkNavListAndIndex();
    if (!list.length || wi < 0) return;
    const target = list[wi + dir];
    if (!target || !target.items.length) return;
    if (dir > 0) {
        openGallery(target.id, 0, -1);
    } else {
        openGallery(target.id, target.items.length - 1, -1);
    }
    ensureWorkCardInDomAndScroll(target.id);
}

/** 懒加载列表卡片直到目标作品出现在 DOM，再滚到可见 */
function ensureWorkCardInDomAndScroll(workId) {
    const list = getFilteredSortedWorks();
    const idx = list.findIndex((w) => w.id === workId);
    if (mbVirtual.active && idx >= 0) {
        renderVirtualSlice(Math.max(0, idx - 8));
        requestAnimationFrame(() => {
            const card = document.querySelector('.work-card[data-id="' + CSS.escape(workId) + '"]');
            if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        });
        return;
    }
    if (idx < 0) {
        const card0 = document.querySelector('.work-card[data-id="' + CSS.escape(workId) + '"]');
        if (card0) card0.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        return;
    }
    for (let n = 0; n < 200; n++) {
        const card = document.querySelector('.work-card[data-id="' + CSS.escape(workId) + '"]');
        if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            return;
        }
        if (renderOffset >= list.length) return;
        renderMoreWorks(list);
    }
}

function navigate(dir) {
    const work = allWorks.find(w => w.id === galleryState.workId);
    if (!work) return;
    const newIdx = galleryState.itemIdx + dir;
    if (newIdx >= 0 && newIdx < work.items.length) {
        galleryState.itemIdx = newIdx;
        galleryState.thumbIdx = -1;
        renderGallery();
        return;
    }
    if (newIdx >= work.items.length && dir > 0) {
        openAdjacentWorkFromGallery(1);
        return;
    }
    if (newIdx < 0 && dir < 0) {
        openAdjacentWorkFromGallery(-1);
        return;
    }
}

/**
 * 删除成功/失败后，画廊应落在的「下一屏」位置（与成功删除后的观感一致）。
 * @param {object} work 当前作品
 * @param {boolean} itemRemoved 是否已从 work.items 移除该文件
 */
function advanceGalleryAfterDeleteAttempt(work, itemRemoved) {
    if (!work || !work.items.length) return;
    const i = galleryState.itemIdx;
    if (itemRemoved) {
        if (galleryState.itemIdx >= work.items.length) {
            galleryState.itemIdx = work.items.length - 1;
        }
        galleryState.thumbIdx = -1;
        renderGallery();
        return;
    }
    if (work.items.length > 1) {
        if (i < work.items.length - 1) {
            galleryState.itemIdx = i + 1;
        } else {
            galleryState.itemIdx = i - 1;
        }
        galleryState.thumbIdx = -1;
        renderGallery();
        return;
    }
    const nav = galleryWorkNavListAndIndex();
    if (nav.wi >= 0 && nav.wi < nav.list.length - 1) {
        openAdjacentWorkFromGallery(1);
        return;
    }
    galleryState.thumbIdx = -1;
    renderGallery();
}

async function deleteCurrentItem() {
    const work = allWorks.find(w => w.id === galleryState.workId);
    if (!work) return;
    const item = work.items[galleryState.itemIdx];
    if (!item) return;

    let deleteOk = false;
    try {
        const res = await fetch('/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({ path: item.path }),
        });
        const data = await res.json();
        if (!data.ok) {
            let msg = '删除失败: ' + (data.error || '未知错误');
            if (data.queued) msg += '\\n已加入废纸篓清单（当前 ' + (data.trash_count || 0) + ' 项），可点页眉「废纸篓」批量重试。';
            alert(msg);
            mbRefreshTrashBadge(true);
            advanceGalleryAfterDeleteAttempt(work, false);
            return;
        }
        deleteOk = true;
    } catch (e) {
        alert('删除失败: ' + e.message);
        advanceGalleryAfterDeleteAttempt(work, false);
        return;
    }

    if (!deleteOk) return;

    work.items.splice(galleryState.itemIdx, 1);
    if (item.type === 'video') work.video_count--;
    else work.image_count--;

    if (work.items.length === 0) {
        const idx = allWorks.findIndex(w => w.id === work.id);
        if (idx >= 0) allWorks.splice(idx, 1);
        closeModal();
        sortAndRenderAll();
        return;
    }

    advanceGalleryAfterDeleteAttempt(work, true);
}

function closeModal() {
    const modal = document.getElementById('modal');
    modal.classList.remove('active');
    setTimeout(() => { document.getElementById('modalMedia').innerHTML = ''; }, 250);
    document.body.style.overflow = '';
    galleryState = { workId: null, itemIdx: 0 };
}

function setupImageZoom() {
    const wrap = document.getElementById('imgWrap');
    const img = document.getElementById('galleryImage');
    if (!wrap || !img) return;
    let scale = 1;
    let panning = false;
    let pointX = 0;
    let pointY = 0;
    let startX = 0;
    let startY = 0;

    function setTransform() {
        img.style.transform = `translate(${pointX}px, ${pointY}px) scale(${scale})`;
        img.style.cursor = scale > 1 ? 'grab' : 'zoom-in';
    }

    img.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? -0.15 : 0.15;
        const newScale = Math.min(5, Math.max(1, scale + delta));
        if (newScale !== scale) {
            scale = newScale;
            if (scale === 1) { pointX = 0; pointY = 0; }
            setTransform();
        }
    }, { passive: false });

    img.addEventListener('mousedown', (e) => {
        if (scale <= 1) return;
        e.preventDefault();
        panning = true;
        startX = e.clientX - pointX;
        startY = e.clientY - pointY;
        img.style.cursor = 'grabbing';
    });

    window.addEventListener('mousemove', (e) => {
        if (!panning) return;
        pointX = e.clientX - startX;
        pointY = e.clientY - startY;
        setTransform();
    });

    window.addEventListener('mouseup', () => {
        if (panning) {
            panning = false;
            img.style.cursor = scale > 1 ? 'grab' : 'zoom-in';
        }
    });

    img.addEventListener('dblclick', () => {
        scale = 1;
        pointX = 0;
        pointY = 0;
        setTransform();
    });

    let pinchStart = 0;
    function td(a, b) {
        const dx = a.clientX - b.clientX;
        const dy = a.clientY - b.clientY;
        return Math.sqrt(dx * dx + dy * dy);
    }
    wrap.addEventListener('touchstart', (e) => {
        if (e.touches.length === 2) pinchStart = td(e.touches[0], e.touches[1]);
    }, { passive: true });
    wrap.addEventListener('touchmove', (e) => {
        if (e.touches.length !== 2 || !pinchStart) return;
        const d = td(e.touches[0], e.touches[1]);
        const ratio = d / pinchStart;
        pinchStart = d;
        const newScale = Math.min(5, Math.max(1, scale * ratio));
        if (newScale !== scale) {
            scale = newScale;
            if (scale === 1) { pointX = 0; pointY = 0; }
            setTransform();
        }
        e.preventDefault();
    }, { passive: false });
    wrap.addEventListener('touchend', (e) => {
        if (e.touches.length < 2) pinchStart = 0;
    }, { passive: true });
}

function toggleGalleryFullscreen() {
    if (document.fullscreenElement) {
        document.exitFullscreen();
    } else {
        document.getElementById('modal').requestFullscreen();
    }
}

// 事件委托：卡片列表中的"标为待审"按钮
document.addEventListener('click', (e) => {
    const pendingBtn = e.target.closest('.review-to-pending');
    if (pendingBtn) {
        e.stopPropagation();
        const workId = pendingBtn.dataset.workId;
        if (workId) setWorkReviewPending(workId);
        return;
    }
    // 画廊中的审阅切换按钮
    const reviewBtn = e.target.closest('#galleryReviewBtn');
    if (reviewBtn) {
        e.stopPropagation();
        const workId = reviewBtn.dataset.workId;
        if (workId) toggleWorkReview(workId);
        return;
    }
});

document.addEventListener('keydown', (e) => {
    if (mbTrashOverlay && mbTrashOverlay.classList.contains('active')) {
        if (e.key === 'Escape') {
            e.preventDefault();
            mbCloseTrashPanel();
            return;
        }
    }
    const modal = document.getElementById('modal');
    if (!modal || !modal.classList.contains('active')) {
        if (typeof activeTab !== 'undefined' && activeTab === 'review') {
            const el = e.target;
            if (el && el.closest && !el.closest('input, textarea, select, [contenteditable="true"]')) {
                handleReviewListFolderNavKeys(e);
            }
        }
        return;
    }
    const v = document.getElementById('galleryVideo');
    const img = document.getElementById('galleryImage');

    // 全局
    if (e.key === 'Escape') { closeModal(); return; }
    if ((e.key === 'i' || e.key === 'I') && (e.metaKey || e.ctrlKey) && e.shiftKey) {
        e.preventDefault();
        deleteCurrentWork();
        return;
    }
    if ((e.key === 'i' || e.key === 'I') && (e.metaKey || e.ctrlKey) && !e.shiftKey) {
        e.preventDefault();
        deleteCurrentItem();
        return;
    }
    // ⌘/Ctrl + 左右：切换作品（不进视频快进/图片翻张）
    if ((e.metaKey || e.ctrlKey) && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
        e.preventDefault();
        if (e.key === 'ArrowRight') openAdjacentWorkFromGallery(1);
        else openAdjacentWorkFromGallery(-1);
        return;
    }

    // 视频播放控制
    if (v) {
        if (e.key === ' ' || e.key === 'k' || e.key === 'K') {
            e.preventDefault();
            v.paused ? v.play() : v.pause();
            return;
        }
        if (e.key === 'f' || e.key === 'F') {
            e.preventDefault();
            if (document.fullscreenElement) {
                document.exitFullscreen();
            } else {
                v.requestFullscreen();
            }
            return;
        }
        if (e.key === 'm' || e.key === 'M') {
            e.preventDefault();
            v.muted = !v.muted;
            return;
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
            v.volume = Math.min(1, v.volume + 0.1);
            return;
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            v.volume = Math.max(0, v.volume - 0.1);
            return;
        }
        // Shift + 左右 = 翻页；纯左右 = 快进/快退
        if (e.key === 'ArrowLeft') {
            if (e.shiftKey) { navigate(-1); }
            else { e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 5); }
            return;
        }
        if (e.key === 'ArrowRight') {
            if (e.shiftKey) { navigate(1); }
            else { e.preventDefault(); v.currentTime = Math.min(v.duration || Infinity, v.currentTime + 5); }
            return;
        }
        if (e.key === 'j' || e.key === 'J') {
            e.preventDefault();
            v.currentTime = Math.max(0, v.currentTime - 10);
            return;
        }
        if (e.key === 'l' || e.key === 'L') {
            e.preventDefault();
            v.currentTime = Math.min(v.duration || Infinity, v.currentTime + 10);
            return;
        }
        // 逐帧（暂停时）
        if (e.key === ',' || e.key === '<') {
            e.preventDefault();
            if (v.paused && v.readyState >= 2) {
                v.currentTime = Math.max(0, v.currentTime - (1 / (v.fps || 30)));
            }
            return;
        }
        if (e.key === '.' || e.key === '>') {
            e.preventDefault();
            if (v.paused && v.readyState >= 2) {
                v.currentTime = Math.min(v.duration || Infinity, v.currentTime + (1 / (v.fps || 30)));
            }
            return;
        }
    }

    // 图片键盘控制（左右翻页）
    if (img) {
        if (e.key === 'ArrowLeft') { navigate(-1); return; }
        if (e.key === 'ArrowRight') { navigate(1); return; }
    }
});

let touchStartX = 0;
let touchStartY = 0;
let touchEndX = 0;
document.getElementById('modalMain').addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length !== 1) return;
    touchStartX = e.touches[0].screenX;
    touchStartY = e.touches[0].screenY;
}, { passive: true });
document.getElementById('modalMain').addEventListener('touchend', (e) => {
    if (!e.changedTouches || e.changedTouches.length !== 1) return;
    touchEndX = e.changedTouches[0].screenX;
    const touchEndY = e.changedTouches[0].screenY;
    const dx = touchStartX - touchEndX;
    const dy = touchStartY - touchEndY;
    if (Math.abs(dx) > 120 && Math.abs(dy) < 55) {
        if (dx > 0) openAdjacentWorkFromGallery(1);
        else openAdjacentWorkFromGallery(-1);
    } else if (Math.abs(dx) > 50) {
        if (dx > 0) navigate(1);
        else navigate(-1);
    }
}, { passive: true });

document.getElementById('modalMain').addEventListener('click', (e) => {
    if (e.target.id === 'modalMain') closeModal();
});

(function mbInitShell() {
    const tc = document.getElementById('worksColToggle');
    const wc = document.getElementById('worksColumn');
    if (tc && wc) {
        tc.addEventListener('click', () => {
            wc.classList.toggle('mb-collapsed');
            tc.setAttribute('aria-expanded', wc.classList.contains('mb-collapsed') ? 'false' : 'true');
        });
    }
    if (mobileTabbar) {
        mobileTabbar.querySelectorAll('[data-mtab]').forEach((btn) => {
            btn.addEventListener('click', () => {
                mobileTabbar.querySelectorAll('[data-mtab]').forEach((b) => {
                    b.classList.toggle('active', b === btn);
                });
                const t = btn.getAttribute('data-mtab');
                if (t === 'works') {
                    closeModal();
                    if (reviewShell) reviewShell.scrollIntoView({ block: 'start' });
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                } else if (t === 'gallery') {
                    const ok = mbLastGalleryWorkId && allWorks.some((x) => x.id === mbLastGalleryWorkId);
                    if (ok) openGallery(mbLastGalleryWorkId, 0, -1);
                    else {
                        const fl = getFilteredSortedWorks();
                        if (fl.length) openGallery(fl[0].id, 0, -1);
                        else alert('暂无作品');
                    }
                } else if (t === 'settings') {
                    const hb = document.getElementById('headerBar');
                    if (hb) hb.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    const inp = document.getElementById('scanRootInput');
                    if (inp) setTimeout(() => inp.focus(), 450);
                }
            });
        });
    }
    if (cardCtxMenu) {
        cardCtxMenu.addEventListener('click', (e) => {
            const b = e.target.closest('[data-ctx-act]');
            if (!b || !mbCtxWork) return;
            e.preventDefault();
            e.stopPropagation();
            const act = b.getAttribute('data-ctx-act');
            const w = mbCtxWork;
            if (act === 'open') openGallery(w.id, 0, -1);
            else if (act === 'finder') fetch('/open?path=' + encodeURIComponent(w.path));
            else if (act === 'rename') alert('请在访达 / 资源管理器中重命名该作品文件夹，回到页眉点击「应用并扫描」刷新媒体库。');
            else if (act === 'rescan') {
                selectedIds.clear();
                selectedIds.add(w.id);
                updateBatchBar();
                switchTab('analysis');
                sortAndRenderAll();
                const ti = document.getElementById('taskNameInput');
                if (ti) ti.focus();
                alert('已勾选该作品并切换到「视频分析任务」：填写任务名后点「用当前勾选创建任务」，即可重新跑 AI 标签。');
            } else if (act === 'delete') {
                deleteWorkAllFiles(w);
                return;
            }
            hideCardCtx();
        });
    }
    document.addEventListener('click', (e) => {
        if (cardCtxMenu && cardCtxMenu.style.display === 'block' && !e.target.closest('#cardCtxMenu')) hideCardCtx();
    });
})();

(function restore() {
    const s = localStorage.getItem('mb_search');
    const f = localStorage.getItem('mb_filter');
    const so = localStorage.getItem('mb_sort');
    if (s !== null) searchInput.value = s;
    if (f !== null) {
        filterType = f;
        document.querySelectorAll('.filters button').forEach(b => {
            b.classList.toggle('active', b.dataset.filter === f);
        });
    }
    if (so !== null) { sortKey = so; sortSelect.value = so; }
    const resetReviewBtn = document.getElementById('resetAllReviewTags');
    if (resetReviewBtn) {
        resetReviewBtn.addEventListener('click', (ev) => {
            ev.preventDefault();
            resetAllReviewTags();
        });
    }
    const applyScanRoot = document.getElementById('applyScanRoot');
    const scanRootInput = document.getElementById('scanRootInput');
    const exitAppBtn = document.getElementById('exitApp');
    if (exitAppBtn) {
        exitAppBtn.addEventListener('click', async () => {
            if (!confirm('确定退出 Media Browser？\\n将停止扫描并关闭本地服务（打包版会退出整个应用）。')) return;
            try {
                await fetch('/api/shutdown', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}',
                });
            } catch (e) {}
            alert('已退出。可关闭此标签页；若从终端启动，终端应已返回提示符。');
        });
    }
    if (applyScanRoot && scanRootInput) {
        applyScanRoot.addEventListener('click', async () => {
            const path = scanRootInput.value.trim();
            if (!path) { alert('请输入目录路径'); return; }
            try {
                const res = await fetch('/api/set-scan-root', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path }),
                });
                const data = await res.json();
                if (!data.ok) { alert(data.error || '设置失败'); return; }
                scanRootInput.value = data.path;
                pollGen++;
                since = 0;
                allWorks = [];
                scanPollDone = false;
                lastEnumError = null;
                selectedIds.clear();
                progressFill.style.width = '0%';
                progressFill.style.background = '';
                sortAndRenderAll();
                poll();
                mbRefreshTrashBadge(true);
            } catch (e) {
                alert(e.message || String(e));
            }
        });
    }
    const tabReview = document.getElementById('tabReview');
    const tabAnalysis = document.getElementById('tabAnalysis');
    const createTaskBtn = document.getElementById('createTaskBtn');
    const refreshTasksBtn = document.getElementById('refreshTasksBtn');
    const saveEvalBtn = document.getElementById('saveEvalBtn');
    if (tabReview) tabReview.addEventListener('click', () => switchTab('review'));
    if (tabAnalysis) tabAnalysis.addEventListener('click', () => switchTab('analysis'));
    if (createTaskBtn) createTaskBtn.addEventListener('click', createAnalysisTask);
    if (refreshTasksBtn) refreshTasksBtn.addEventListener('click', loadTaskList);
    if (saveEvalBtn) saveEvalBtn.addEventListener('click', saveTaskEvaluation);
    // 任务表按钮使用内联 onclick，需要挂到全局
    window.previewAnalysisTask = previewAnalysisTask;
    window.executeAnalysisTask = executeAnalysisTask;
    window.rollbackAnalysisTask = rollbackAnalysisTask;
    window.useTaskForEval = useTaskForEval;
    window.startAiAnalyze = startAiAnalyze;
    window.openInsightPanel = openInsightPanel;
    const loadInsightBtn = document.getElementById('loadInsightBtn');
    const saveConfirmBtn = document.getElementById('saveConfirmBtn');
    if (loadInsightBtn) loadInsightBtn.addEventListener('click', () => loadTaskDetail(false));
    if (saveConfirmBtn) saveConfirmBtn.addEventListener('click', saveInsightConfirms);
    const mbTrashClose = document.getElementById('mbTrashClose');
    if (mbTrashBtn) mbTrashBtn.addEventListener('click', () => { mbOpenTrashPanel(); });
    if (mbTrashClose) mbTrashClose.addEventListener('click', mbCloseTrashPanel);
    if (mbTrashOverlay) mbTrashOverlay.addEventListener('click', (ev) => {
        if (ev.target === mbTrashOverlay) mbCloseTrashPanel();
    });
    const mbTrashRetryAllBtn = document.getElementById('mbTrashRetryAll');
    if (mbTrashRetryAllBtn) mbTrashRetryAllBtn.addEventListener('click', mbTrashRetryAllAction);
    const mbTrashSelectAll = document.getElementById('mbTrashSelectAll');
    if (mbTrashSelectAll && !mbTrashSelectAll.dataset.mbBound) {
        mbTrashSelectAll.dataset.mbBound = '1';
        mbTrashSelectAll.addEventListener('change', function () {
            if (!mbTrashListWrap) return;
            const on = mbTrashSelectAll.checked;
            mbTrashListWrap.querySelectorAll('.mb-trash-row-chk').forEach(function (c) { c.checked = on; });
            mbTrashUpdateSelectionUi();
        });
    }
    const mbTrashDeleteSelectedBtn = document.getElementById('mbTrashDeleteSelected');
    if (mbTrashDeleteSelectedBtn && !mbTrashDeleteSelectedBtn.dataset.mbBound) {
        mbTrashDeleteSelectedBtn.dataset.mbBound = '1';
        mbTrashDeleteSelectedBtn.addEventListener('click', function () { mbTrashDeleteSelectedAction(); });
    }
    setTimeout(function () { mbRefreshTrashBadge(true); }, 600);
    switchTab('review');
})();

poll();
</script>
</body>
</html>
'''




def main():
    global _http_server
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
    if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical"):
        logger.info(
            "MB_DISK_PROFILE=%s：已限制并发与缩略图帧数以减轻硬盘负载",
            DISK_PROFILE,
        )
    logger.info(
        "监听 %s:%s（本机访问 http://localhost:%s；仅本机请设 MB_HOST=127.0.0.1）",
        HOST,
        PORT,
        PORT,
    )
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
            webbrowser.open(f"http://127.0.0.1:{PORT}/")

        threading.Thread(target=_open_browser, daemon=True).start()

    logger.info("按 Ctrl+C 停止")
    scanner.start()
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
