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

完整说明（功能、环境变量、打包、路线图）见项目根目录 README.md。
"""

import os
import sys
import json
import hashlib
import threading
import subprocess
import time
import mimetypes
import re
import html as html_mod
import shutil
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 配置 =====================
APP_VERSION = "1.0.3"


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
    ROOT_DIR = os.environ.get("MB_ROOT_DIR", "/Volumes/Untitled/pri")
    CACHE_DIR = os.environ.get(
        "MB_CACHE_DIR", os.path.expanduser("~/.cache/media-browser/thumbs")
    )

HOST = os.environ.get("MB_HOST", "0.0.0.0")
PORT = int(os.environ.get("MB_PORT", "8765"))
THUMB_WIDTH = 400

# 并发默认 2（原 4）：多任务并行会对 NAS/机械盘产生大量随机寻道；SSD 可用 MB_SCAN_WORKERS=4
DISK_PROFILE = os.environ.get("MB_DISK_PROFILE", "").strip().lower()
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


def remove_media_thumb_cache(media_path: str) -> None:
    """删除该媒体在缓存目录下对应的缩略图文件夹（与 generate_* 使用的 sha256(abspath) 一致）。"""
    try:
        key = sha256_str(os.path.abspath(media_path))
        thumb_dir = os.path.join(CACHE_DIR, key)
        if os.path.isdir(thumb_dir):
            shutil.rmtree(thumb_dir, ignore_errors=True)
    except Exception:
        pass


def get_video_info(path: str) -> dict:
    info = {"duration": 0.0, "width": 0, "height": 0}
    _probe_timeout = 90 if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical") else 45
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "error",
             "-show_entries", "format=duration",
             "-show_entries", "stream=width,height",
             "-select_streams", "v:0",
             "-of", "json", path],
            capture_output=True, text=True, timeout=_probe_timeout
        )
        data = json.loads(out.stdout)
        if "format" in data and "duration" in data["format"]:
            info["duration"] = float(data["format"]["duration"])
        if "streams" in data and len(data["streams"]) > 0:
            s = data["streams"][0]
            info["width"] = s.get("width", 0)
            info["height"] = s.get("height", 0)
    except Exception:
        pass
    return info


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
                print(f"[ffmpeg thumb error] {video_path} @ {ss:.1f}s: {err}")
        except Exception as e:
            print(f"[ffmpeg thumb exception] {video_path} @ {ss:.1f}s: {e}")

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
            print(f"[深层兜底枚举错误] {e}")
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
            print(f"[枚举错误] {e}")
            self.enum_error = str(e)
        if not candidates and not root_flat:
            self._enumerate_deep_fallback(candidates, root_flat)
            if candidates or root_flat:
                print(
                    f"[深层兜底] 子文件夹 {len(candidates)} 个，根目录媒体文件 {len(root_flat)} 个"
                )
        self._pending_root_files = sorted(root_flat)
        self.pending_works = sorted(candidates)
        extra = 1 if root_flat else 0
        self.total_dirs = len(self.pending_works) + extra
        msg = f"[枚举完成] 子文件夹作品 {len(candidates)} 个"
        if root_flat:
            msg += f"，根目录平铺媒体 {len(root_flat)} 个（将单独显示为一个作品）"
        print(msg)

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
                print(f"[扫描错误] {label}: {e}")
                self.scanned_dirs += 1

        self.done = True
        print("[扫描完成]")

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
            print(f"[process root_flat error]: {e}")
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
            return self._build_work_from_items(items, work_path, None)
        except Exception as e:
            print(f"[process error] {work_path}: {e}")
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
    old = scanner
    scanner = MediaScanner()
    scanner.start()
    try:
        old._executor.shutdown(wait=False)
    except Exception:
        pass
    return True


scanner = MediaScanner()

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


class AnalysisTaskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}

    def list_tasks(self):
        with self._lock:
            rows = []
            for t in self._tasks.values():
                rows.append({
                    "id": t["id"],
                    "name": t["name"],
                    "status": t["status"],
                    "created_at": t["created_at"],
                    "file_count": len(t["source_files"]),
                    "preview_count": len(t["preview"]),
                    "executed_count": len(t["executed"]),
                    "mapping_file": t.get("mapping_file", ""),
                })
            rows.sort(key=lambda x: x["created_at"], reverse=True)
            return rows

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
        task = {
            "id": tid,
            "name": (name or "").strip() or f"分析任务-{tid}",
            "status": "draft",
            "created_at": now,
            "source_files": selected,
            "preview": [],
            "executed": [],
            "mapping_file": "",
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
            by_dir = {}
            for p in task["source_files"]:
                by_dir.setdefault(os.path.dirname(p), []).append(p)
            preview = []
            for d, files in by_dir.items():
                files = sorted(files)
                seq = 1
                used = set(os.listdir(d))
                for src in files:
                    while True:
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
            return {"id": task["id"], "preview": preview, "file_count": len(task["source_files"])}

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

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
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

        elif path == "/api/progress":
            since = int(qs.get("since", ["0"])[0])
            prog = scanner.get_progress(since)
            self._send_json(prog)
        elif path == "/api/tasks":
            self._send_json({"ok": True, "tasks": analysis_tasks.list_tasks()})

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
                        subprocess.run(["open", target], timeout=10, capture_output=True)
                        self._send_json({"ok": True})
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
        m = re.fullmatch(r"/api/tasks/([0-9a-fA-F]+)/preview", parsed.path or "")
        if m:
            ret = analysis_tasks.build_preview(m.group(1))
            if not ret:
                self._send_json({"ok": False, "error": "task not found"}, 404)
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
            self._send_json({"ok": False, "error": str(e)}, 500)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    pass

# ===================== 前端页面 =====================
HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Media Browser v__APP_VERSION__</title>
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
.image-row img {
    height: 120px;
    width: auto;
    object-fit: cover;
    border-radius: 4px;
    cursor: pointer;
}
.video-thumbs-panel {
    padding: 8px;
    border-bottom: 1px solid #222;
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
.modal-main .video-wrap {
    position: relative;
    display: inline-block;
    max-width: 100%;
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
.modal-main video, .modal-main img {
    max-width: 100%;
    max-height: calc(100vh - 140px);
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
    width: 260px;
    background: #111;
    border-left: 1px solid #222;
    display: flex;
    flex-direction: column;
    padding: 12px;
    gap: 10px;
    overflow-y: auto;
}
.modal-sidebar h3 {
    font-size: 13px;
    color: #888;
    font-weight: 500;
    margin-bottom: 4px;
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
    right: 280px;
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

@media (max-width: 900px) {
    .modal-sidebar { display: none; }
    .modal-close { right: 12px; }
    #container { padding: 12px; }
    header input[type="text"] { width: 100%; }
    header .brand { max-width: 100%; }
}
</style>
</head>
<body>
<div class="progress-bar"><div class="fill" id="progressFill"></div></div>
<header>
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
    <button type="button" id="resetAllReviewTags" class="review-reset-all" title="将所有作品的待审/保留标记清空为「待审」">标记全重置</button>
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
            范围规则：仅同目录改名，禁止跨目录移动；执行前先预览并生成映射备份；命名末尾自动追加序号，降低冲突风险。
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
    <div class="analysis-card">
        <h3>预览（最近一次）</h3>
        <div id="analysisPreview" class="analysis-note">尚未生成预览。</div>
    </div>
    <div class="analysis-card">
        <h3>效果打分（人工认可率 + Top-K 命中率）</h3>
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

<div id="container"></div>
<div id="emptyHint" class="empty-hint" style="display:none;" aria-live="polite"></div>

<div id="status">准备扫描...</div>

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

<!-- 画廊模态框 -->
<div class="modal" id="modal">
    <div class="modal-body">
        <div class="modal-main" id="modalMain">
            <div class="modal-close" onclick="closeModal()">&times;</div>
            <div class="modal-nav prev" onclick="navigate(-1)">&#10094;</div>
            <div class="modal-nav next" onclick="navigate(1)">&#10095;</div>
            <div id="modalMedia"></div>
            <div class="file-info" id="modalFileInfo"></div>
            <div class="shortcut-hint" id="shortcutHint">← → 翻页 · ESC 关闭 · 空格 播放/暂停 · ⌘I / Ctrl+I 删除</div>
        </div>
        <div class="modal-sidebar" id="modalSidebar">
            <h3 id="sidebarTitle">作品文件</h3>
            <div class="file-list" id="fileList"></div>
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
const TRANSCODE_VIDEO_EXTS = new Set(['.avi','.ts','.mts','.m2ts','.wmv','.vob','.flv']);
function buildVideoPlayUrl(filePath) {
    const i = filePath.lastIndexOf('.');
    const ext = i >= 0 ? filePath.slice(i).toLowerCase() : '';
    if (TRANSCODE_VIDEO_EXTS.has(ext)) {
        return '/play?path=' + encodeURIComponent(filePath);
    }
    return '/file?path=' + encodeURIComponent(filePath);
}
function getWorkReviewTag(workId) {
    try {
        const o = JSON.parse(localStorage.getItem('mb_review_tags') || '{}');
        return o[workId] === 'kept' ? 'kept' : 'pending';
    } catch (e) { return 'pending'; }
}
function buildReviewStripHtml(workId) {
    const kept = getWorkReviewTag(workId) === 'kept';
    const btn = kept ? '<button type="button" class="review-to-pending" onclick="event.stopPropagation(); setWorkReviewPending(' + JSON.stringify(workId) + ')">标为待审</button>' : '';
    const cls = kept ? 'kept' : 'pending';
    const txt = kept ? '保留' : '待审';
    return '<div class="review-strip"><span class="review-tag ' + cls + '">' + txt + '</span>' + btn + '</div>';
}
function setWorkReviewPending(workId) {
    try {
        const o = JSON.parse(localStorage.getItem('mb_review_tags') || '{}');
        o[workId] = 'pending';
        localStorage.setItem('mb_review_tags', JSON.stringify(o));
    } catch (e) {}
    const card = document.querySelector('.work-card[data-id="' + workId + '"]');
    if (card) {
        const strip = card.querySelector('.review-strip');
        if (strip) strip.outerHTML = buildReviewStripHtml(workId);
    }
}
function markWorkOpenedKept(workId) {
    try {
        const o = JSON.parse(localStorage.getItem('mb_review_tags') || '{}');
        o[workId] = 'kept';
        localStorage.setItem('mb_review_tags', JSON.stringify(o));
    } catch (e) {}
    const card = document.querySelector('.work-card[data-id="' + workId + '"]');
    if (card) {
        const strip = card.querySelector('.review-strip');
        if (strip) strip.outerHTML = buildReviewStripHtml(workId);
    }
}
function resetAllReviewTags() {
    if (!confirm('将所有作品的标记恢复为「待审」？')) return;
    localStorage.removeItem('mb_review_tags');
    sortAndRenderAll();
}
function buildThumbUrl(cachePath) {
    if (!cachePath) return '';
    const parts = cachePath.split('/');
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
    document.getElementById('analysisPanel').classList.toggle('active', !reviewVisible);
    document.getElementById('tabReview').classList.toggle('active', reviewVisible);
    document.getElementById('tabAnalysis').classList.toggle('active', !reviewVisible);
    if (!reviewVisible) loadTaskList();
}

async function loadTaskList() {
    const rows = document.getElementById('analysisTaskRows');
    const evalSel = document.getElementById('evalTaskSelect');
    try {
        const r = await fetch('/api/tasks');
        const data = await r.json();
        if (!data.ok) throw new Error(data.error || '加载任务失败');
        if (evalSel) {
            const oldVal = evalSel.value || '';
            evalSel.innerHTML = '<option value="">选择任务后再保存评分</option>' + (data.tasks || []).map(t =>
                `<option value="${t.id}">${escapeHtml(t.name)} (${t.id})</option>`
            ).join('');
            if (oldVal && (data.tasks || []).some(t => t.id === oldVal)) evalSel.value = oldVal;
        }
        if (!data.tasks || data.tasks.length === 0) {
            rows.innerHTML = '<tr><td colspan="5" style="color:#777;">暂无任务</td></tr>';
            return;
        }
        rows.innerHTML = data.tasks.map(t => {
            const map = t.mapping_file ? `<a href="#" onclick="event.preventDefault(); copyText(${JSON.stringify(t.mapping_file)});">复制路径</a>` : '-';
            const e = t.eval || {};
            const evalText = `人工通过率 ${Number(e.human_pass_rate || 0).toFixed(2)}% / Top-K ${Number(e.topk_hit_rate || 0).toFixed(2)}%`;
            return `<tr>
                <td>${escapeHtml(t.name)}<div style="color:#666;font-size:11px;">${t.id}</div></td>
                <td>${escapeHtml(t.status)}</td>
                <td>${t.file_count}</td>
                <td>${map}</td>
                <td>
                    <button onclick="previewAnalysisTask('${t.id}')">预览</button>
                    <button onclick="executeAnalysisTask('${t.id}')">执行</button>
                    <button onclick="rollbackAnalysisTask('${t.id}')">回滚</button>
                    <button onclick="useTaskForEval('${t.id}')">评测</button>
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

function createWorkCard(work) {
    const div = document.createElement('div');
    div.className = 'work-card';
    div.dataset.id = work.id;

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
                const onclick = `event.stopPropagation(); openGallery(${JSON.stringify(work.id)}, ${i}, ${j})`;
                contentHtml += `<div class="thumb" onclick="${onclick.replace(/"/g,'&quot;')}">
                    <img src="${url}" loading="lazy" alt="" onload="this.classList.add('loaded')" onerror="this.classList.add('loaded'); this.style.opacity='0.3';">
                    <div class="loader"></div>
                    ${isPh ? '<div class="ph-icon">❓</div>' : ''}
                </div>`;
            }
            contentHtml += `</div></div>`;
        } else {
            const url = buildThumbUrl(it.thumb);
            contentHtml += `<div class="image-row" onclick="event.stopPropagation(); openGallery(${JSON.stringify(work.id)}, ${i}, 0)">
                <img src="${url}" loading="lazy" alt="" onload="this.classList.add('loaded')" onerror="this.classList.add('loaded'); this.style.opacity='0.3';">
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

    div.addEventListener('click', (e) => {
        if (e.target.closest('.chk') || e.target.closest('.title') || e.target.closest('.thumb') || e.target.closest('.image-row') || e.target.closest('.review-strip')) return;
        openGallery(work.id, 0, -1);
    });

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
    container.innerHTML = '';
    displayedIds.clear();
    renderOffset = 0;
    const list = getFilteredSortedWorks();
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
    renderMoreWorks(list);
}

function renderMoreWorks(list) {
    const slice = list.slice(renderOffset, renderOffset + BATCH_STEP);
    for (const w of slice) {
        if (displayedIds.has(w.id)) continue;
        displayedIds.add(w.id);
        container.appendChild(createWorkCard(w));
    }
    renderOffset += slice.length;
}

let scrollTimer = null;
window.addEventListener('scroll', () => {
    if (window.scrollY > 400) backToTopBtn.classList.add('show');
    else backToTopBtn.classList.remove('show');
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
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
        mediaDiv.innerHTML = `<div class="video-wrap">${overlayHtml}<video id="galleryVideo" src="${url}" controls preload="metadata" playsinline style="max-width:85vw;max-height:72vh;"></video></div>`;
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
    } else {
        mediaDiv.innerHTML = `<img src="${url}" style="max-width:85vw;max-height:72vh;" />`;
    }

    const dur = item.duration ? `⏱ ${fmtDuration(item.duration)} · ` : '';
    const delBtn = `<span onclick="event.stopPropagation(); deleteCurrentItem();" style="cursor:pointer;color:#ff4444;margin-left:12px;font-size:12px;" title="删除：⌘I 或 Ctrl+I">🗑 删除</span>`;
    infoDiv.innerHTML = `${dur}${escapeHtml(item.name)} · ${fmtSize(item.size)}${delBtn}`;
    const sh = document.getElementById('shortcutHint');
    if (sh) sh.textContent = '← → 翻页 · ESC 关闭 · 空格 播放/暂停 · ⌘I / Ctrl+I 删除';
    sidebarTitle.textContent = work.name;

    renderSidebar(fileList, work, item);

    const activeEl = fileList.querySelector('.file-item.active');
    if (activeEl) activeEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderSidebar(fileList, work, currentItem) {
    let html = '';

    if (currentItem && currentItem.type === 'video' && currentItem.thumbs && currentItem.thumbs.length > 0) {
        html += '<div class="video-thumbs-panel"><h4>视频帧</h4><div class="vt-list">';
        for (let i = 0; i < currentItem.thumbs.length; i++) {
            const url = buildThumbUrl(currentItem.thumbs[i]);
            const active = i === galleryState.thumbIdx ? 'active' : '';
            const onclk = `event.stopPropagation(); jumpToThumb(${i})`;
            html += `<div class="vt-item ${active}" onclick="${onclk}">
                <img src="${url}" onload="this.classList.add('loaded')" onerror="this.style.display='none'">
            </div>`;
        }
        html += '</div></div>';
    }

    const total = work.items.length;
    const showLoadMore = total > sidebarLimit && sidebarOffset + sidebarLimit < total;
    const slice = work.items.slice(sidebarOffset, sidebarOffset + sidebarLimit);

    html += '<div class="file-list">';
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
    html += '</div>';
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

function navigate(dir) {
    const work = allWorks.find(w => w.id === galleryState.workId);
    if (!work) return;
    const newIdx = galleryState.itemIdx + dir;
    if (newIdx >= 0 && newIdx < work.items.length) {
        galleryState.itemIdx = newIdx;
        galleryState.thumbIdx = -1;
        renderGallery();
    }
}

async function deleteCurrentItem() {
    const work = allWorks.find(w => w.id === galleryState.workId);
    if (!work) return;
    const item = work.items[galleryState.itemIdx];
    if (!item) return;

    try {
        const res = await fetch('/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({ path: item.path }),
        });
        const data = await res.json();
        if (!data.ok) {
            alert('删除失败: ' + (data.error || '未知错误'));
            return;
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
        return;
    }

    // 从 items 中移除
    work.items.splice(galleryState.itemIdx, 1);

    // 更新计数
    if (item.type === 'video') work.video_count--;
    else work.image_count--;

    // 如果作品空了，移除整个作品
    if (work.items.length === 0) {
        const idx = allWorks.findIndex(w => w.id === work.id);
        if (idx >= 0) allWorks.splice(idx, 1);
        closeModal();
        sortAndRenderAll();
        return;
    }

    // 调整当前索引
    if (galleryState.itemIdx >= work.items.length) {
        galleryState.itemIdx = work.items.length - 1;
    }

    // 刷新画廊
    renderGallery();
}

function closeModal() {
    const modal = document.getElementById('modal');
    modal.classList.remove('active');
    setTimeout(() => { document.getElementById('modalMedia').innerHTML = ''; }, 250);
    document.body.style.overflow = '';
    galleryState = { workId: null, itemIdx: 0 };
}

document.addEventListener('keydown', (e) => {
    const modal = document.getElementById('modal');
    if (!modal.classList.contains('active')) return;
    if (e.key === 'Escape') closeModal();
    if (e.key === 'ArrowLeft') navigate(-1);
    if (e.key === 'ArrowRight') navigate(1);
    if (e.key === ' ') {
        const v = document.getElementById('galleryVideo');
        if (v) { e.preventDefault(); v.paused ? v.play() : v.pause(); }
    }
    if ((e.key === 'i' || e.key === 'I') && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        deleteCurrentItem();
    }
});

let touchStartX = 0;
let touchEndX = 0;
document.getElementById('modalMain').addEventListener('touchstart', (e) => {
    touchStartX = e.changedTouches[0].screenX;
}, { passive: true });
document.getElementById('modalMain').addEventListener('touchend', (e) => {
    touchEndX = e.changedTouches[0].screenX;
    const diff = touchStartX - touchEndX;
    if (Math.abs(diff) > 50) {
        if (diff > 0) navigate(1); else navigate(-1);
    }
}, { passive: true });

document.getElementById('modalMain').addEventListener('click', (e) => {
    if (e.target.id === 'modalMain') closeModal();
});

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
    switchTab('review');
})();

poll();
</script>
</body>
</html>
'''




def main():
    global _http_server
    auto_open = os.environ.get(
        "MB_AUTO_OPEN",
        "1" if getattr(sys, "frozen", False) else "0",
    ).strip().lower() in ("1", "yes", "true", "on")
    print(f"[Media Browser] v{APP_VERSION}")
    print(f"[Media Browser] 扫描目录: {get_scan_root()}")
    print(f"[Media Browser] 缩略图缓存: {CACHE_DIR}")
    print(
        f"[Media Browser] 扫描并发={MAX_WORKERS}，每视频条带缩略图={THUMB_COUNT} "
        f"（可用 MB_SCAN_WORKERS / MB_THUMB_COUNT / MB_DISK_PROFILE 调整）"
    )
    if DISK_PROFILE in ("slow", "nas", "hdd", "mechanical"):
        print(
            f"[Media Browser] MB_DISK_PROFILE={DISK_PROFILE}：已限制并发与缩略图帧数以减轻硬盘负载"
        )
    print(f"[Media Browser] 监听 {HOST}:{PORT}（本机访问 http://localhost:{PORT}；仅本机请设 MB_HOST=127.0.0.1）")
    print("[Media Browser] 浏览器页眉可点「退出应用」停止服务")
    if auto_open:
        print("[Media Browser] 将自动打开浏览器（如需关闭请设 MB_AUTO_OPEN=0）")

        def _open_browser():
            import webbrowser

            time.sleep(1.0)
            webbrowser.open(f"http://127.0.0.1:{PORT}/")

        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"[Media Browser] 按 Ctrl+C 停止")
    scanner.start()
    server = ThreadedHTTPServer((HOST, PORT), Handler)
    _http_server = server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出中...]")
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
