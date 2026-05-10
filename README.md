# Media Browser

本地视频/图片流式浏览与轻量审阅（单文件 **`media_browser.py`**）。依赖 **ffmpeg**、**ffprobe**（脚本用 PATH；`.app` 会捆绑构建时的可执行文件）。

**当前版本**以 `media_browser.py` 中 `APP_VERSION` 与页眉 `v…` 为准（当前为 **1.0.13**，发版前请与打包配置核对）。

---

## 目录

1. [快速开始](#快速开始)  
2. [Docker 部署](#docker-部署)  
3. [环境变量](#环境变量)  
4. [功能说明](#功能说明)  
5. [性能与硬盘友好](#性能与硬盘友好)  
6. [HTTP 接口](#http-接口)  
7. [本地数据（localStorage）](#本地数据localstorage)  
8. [macOS 打包与 DMG](#macos-打包与-dmg)  
9. [路线图与待办](#路线图与待办)  
10. [注意事项](#注意事项)  

---

## 快速开始

```bash
python3 media_browser.py
```

浏览器打开：**http://localhost:8765/**（端口见下表 `MB_PORT`）。

---

## Docker 部署

适合 NAS 或远程服务器运行：

```bash
docker compose up -d --build
```

容器默认以 `uid=1000` 运行。若宿主机 UID 不同，请修改 `docker-compose.yml` 中的 `user` 行（见文件内注释）。

---

## 环境变量

| 变量 | 含义 | 默认（脚本） | 默认（打包 .app） |
|------|------|----------------|-------------------|
| `MB_ROOT_DIR` | 启动时的扫描根目录；运行中可在**页眉**改路径并「应用并扫描」 | macOS: `/Volumes/Untitled/pri`<br>Win/Linux: `~/MediaBrowser` | `~/Documents/MediaBrowser` |
| `MB_CACHE_DIR` | 缩略图缓存目录 | `~/.cache/media-browser/thumbs` | `~/Library/Application Support/Media Browser/thumbs` |
| `MB_OLLAMA_HOST` | 本地 Ollama 地址（AI 视频分析） | `http://127.0.0.1:11434` | 同上 |
| `MB_OLLAMA_MODEL` | 视觉模型名（如 `llava`、`moondream`） | `llava` | 同上 |
| `MB_OLLAMA_TIMEOUT` | Ollama 单次请求超时（秒） | `300` | 同上 |
| `MB_ANALYZE_FRAME_COUNT` | AI 分析时每视频抽帧数（2–12） | `5` | 同上 |
| `MB_PORT` | HTTP 端口 | `8765` | 同上 |
| `MB_HOST` | 监听地址 | `0.0.0.0` | 同上 |
| `MB_AUTO_OPEN` | 启动后是否自动打开浏览器 | `0`（否） | `1`（是）；脚本也可设为 `1` |
| `MB_SCAN_WORKERS` | 同时处理「作品」任务的线程数；默认 **2**，减少对机械盘/NAS 并发随机读 | `2`（慢速盘 profile 下更保守） | 同上 |
| `MB_THUMB_COUNT` | 每个视频**条带**缩略图帧数；越大越慢、读盘越多 | `5`（慢速盘 profile 下更保守） | 同上 |
| `MB_DISK_PROFILE` | 设为 `slow` / `nas` / `hdd` / `mechanical` 时自动收紧并发与条带帧数、略延长 `ffprobe` 超时 | 未设置 | 同上 |

启动时终端会打印当前**扫描并发**与**每视频条带缩略图帧数**。NAS / 外置机械盘示例：

```bash
MB_DISK_PROFILE=nas MB_SCAN_WORKERS=1 MB_THUMB_COUNT=2 python3 media_browser.py
```

---

## 功能说明

### 扫描与「作品」规则

- **一级子文件夹**：枚举**当前扫描根**下的一级目录；其内**任意深度**出现支持的图片/视频扩展名，即为一个「作品」。
- **根目录平铺**：媒体文件若直接躺在扫描根下（无子文件夹），会合并为虚拟作品「**根目录内的媒体（未放入子文件夹）**」。
- **页眉切换路径**：「扫描根目录」输入本机路径（支持 `~`，并对弯引号等做规范化）→「**应用并扫描**」→ 无需重启即可换目录并重扫。`MB_ROOT_DIR` 仅决定**启动时**默认根目录。
- **发现能力**：目录遍历对指向文件夹的**符号链接**会跟随（`followlinks`）；一级枚举无结果时有**整树兜底**分组；跳过 `.Trash` / `.Trashes`；支持常见扩展名（含 **`.mts`** 等）。

### 列表与交互

- **列表**：搜索、排序；筛选 **全部 / 有视频 / 有图片 / 仅待审**（「仅待审」= 尚未标为保留）。
- **懒加载**：滚动追加卡片。
- **画廊**：播放、侧栏文件列表与视频帧条带；支持**全屏**、**图片缩放/拖拽/双击还原**、**视频技术元数据**（分辨率/编码/码率/帧率）显示；丰富的**键盘快捷键**（见下表）。
- **删除**：`POST /delete`，路径须在扫描根下；删除**源文件成功后**，会按 `sha256(abspath)` 删除 `MB_CACHE_DIR` 下对应缩略图目录，避免孤儿缓存。
- **待审 / 保留**：打开画廊后记「保留」；画廊内可直接点击按钮**切换保留/待审**；卡片列表可「标为待审」；页眉「标记全重置」（需确认）。
- **AI 视频分析**（可选）：本机运行 Ollama + 视觉模型（如 `llava`），自动分析视频帧并生成时间/地点/事件/标签建议，支持批量重命名。需先 `ollama pull` 视觉模型。
- **批量**：复选 + 批量保留/待审，或在文件管理器中打开所在文件夹（macOS Finder / Windows 资源管理器 / Linux xdg-open）。
- **难播格式**：走 **`/play`** 实时转码；界面有转码提示。
- **退出**：页眉「**退出应用**」→ `POST /api/shutdown`，停止服务并退出进程。
- **安全**：`/file`、`/open`、`/delete` 校验路径在当前扫描根下。

### 画廊快捷键

| 按键 | 视频 | 图片 |
|------|------|------|
| `←` / `→` | 快退 / 快进 **5 秒** | 上一张 / 下一张 |
| `Shift + ←` / `Shift + →` | 上一个 / 下一个文件 | — |
| `J` / `L` | 快退 / 快进 **10 秒** | — |
| `空格` / `K` | 播放 / 暂停 | — |
| `F` | 视频**全屏** | — |
| `M` | **静音**切换 | — |
| `↑` / `↓` | 音量 **±10%** | — |
| `,` / `.` | **逐帧**后退 / 前进（需暂停） | — |
| `滚轮` | — | **缩放** |
| `拖拽` | — | 放大后**平移** |
| `双击` | — | **还原**原始大小 |
| `ESC` | 关闭画廊 | 关闭画廊 |
| `⌘I` / `Ctrl+I` | 删除当前文件 | 删除当前文件 |

---

## 性能与硬盘友好

- 每个视频入库时 **`ffprobe` 仅一次**，结果供元数据与缩略图共用（避免对外置盘/NAS 重复读文件头）。
- 通过 `MB_SCAN_WORKERS`、`MB_THUMB_COUNT`、`MB_DISK_PROFILE` 控制并发与条带抽帧，减轻机械盘与 NAS 寻道。

---

## HTTP 接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 单页 HTML |
| `/api/progress?since=` | GET | 扫描进度与增量作品（含 `scan_root` 等） |
| `/api/set-scan-root` | POST | JSON `{"path":"/绝对路径"}`，切换扫描根并重新扫描 |
| `/api/shutdown` | POST | 退出进程（body 可为 `{}`） |
| `/thumb/...` | GET | 缩略图 |
| `/file?path=` | GET | 原文件 |
| `/play?path=` | GET | 实时转码 MP4 |
| `/open?path=` | GET | 在文件管理器中打开目录（跨平台） |
| `/api/tasks` | POST | 创建 AI 分析任务 |
| `/api/tasks/{id}/analyze` | POST | 开始 AI 分析 |
| `/api/tasks/{id}/confirm` | POST | 确认分析结果标签 |
| `/api/tasks/{id}/preview` | POST | 预览批量重命名映射 |
| `/api/tasks/{id}/execute` | POST | 执行批量重命名 |
| `/api/tasks/{id}/rollback` | POST | 回滚重命名 |
| `/delete` | POST | JSON `{"path":"…"}`，删文件并清对应缩略图缓存 |

需跨域时服务器会响应 **`OPTIONS`**。

---

## 本地数据（localStorage）

`mb_search`、`mb_sort`、`mb_filter`、`mb_review_tags` 等（审阅相关标签存于本地浏览器，不随删除媒体自动清理「其他作品」的标记，行为同此前设计）。

---

## macOS 打包与 DMG

### 前置条件

- **macOS**、**Python 3**、可执行 **`ffmpeg` / `ffprobe`**（如 `brew install ffmpeg`）。打包会把**当时 PATH 里**的二进制打入 `.app`。

### 发版时版本号三处一致

1. `media_browser.py` → **`APP_VERSION`**  
2. `packaging/MediaBrowser.spec` → **`info_plist`** 中 `CFBundleVersion` / `CFBundleShortVersionString`  
3. `packaging/build_dmg.sh` → **`VERSION`**（决定 `Media Browser v${VERSION}.dmg` 文件名）

### 一键构建

```bash
chmod +x packaging/build_dmg.sh
./packaging/build_dmg.sh
```

脚本要点：在 **`$TMPDIR`（启动磁盘）** 上跑 PyInstaller 的 `workpath`/`distpath` 与 `hdiutil create`，避免项目放在**外置卷**时 `._*` / `codesign` / 权限问题；构建结果同步到项目 **`dist/`**；`COPYFILE_DISABLE=1` 并清理 AppleDouble。若失败，将项目复制到**内置盘路径**再构建。详见脚本内注释。

**典型产出**：`dist/Media Browser.app`、`dist/Media Browser v<VERSION>.dmg`。

### 脚本与 .app 的差异（摘要）

| 项目 | 脚本 | 打包 .app |
|------|------|-----------|
| 默认扫描根 | `MB_ROOT_DIR` 或 `/Volumes/Untitled/pri` | `~/Documents/MediaBrowser`；仍可用页眉改路径 |
| 缓存 | `~/.cache/...` 等 | `~/Library/Application Support/Media Browser/thumbs` |
| ffmpeg | PATH | 优先包内捆绑 |
| 自动开浏览器 | 默认关，`MB_AUTO_OPEN=1` 可开 | 默认开，`MB_AUTO_OPEN=0` 可关 |
| 控制台 | 当前终端 | 双击常为控制台窗口（可在 spec 改 `console=False`） |

> **注意：** macOS 版本为 **Apple Silicon (M1/M2/M3/M4)** 专用。Intel Mac 用户请使用脚本直接运行。

正式分发需自行 **codesign / notarize**（此处不展开）。

---

## Windows 打包

### 前置条件

- **Windows 10/11**、**Python 3.12+**
- `ffmpeg.exe` 和 `ffprobe.exe` 在 PATH 中（可从 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载静态构建）
- PowerShell

### 一键构建

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

**典型产出**：`dist/MediaBrowser-v<VERSION>-windows.zip`

解压后运行 `MediaBrowser.exe` 即可。

---

## GitHub Releases 自动发布

项目已配置 GitHub Actions，推送 `v*` 标签时自动构建并发布：

```bash
git tag v1.0.13
git push origin v1.0.13
```

Actions 会并行构建：
- **macOS Intel** (`macos-13`) → `.dmg`
- **macOS Apple Silicon** (`macos-14`) → `.dmg`
- **Windows** (`windows-latest`) → `.zip`

产物自动上传到该版本的 [GitHub Releases](https://github.com/yangch504855530-afk/media-browser/releases) 页面。

> ⚠️ 首次使用 Actions 前，请确认仓库 Settings → Actions → General → Workflow permissions 已设为 "Read and write permissions"。

---

## 路线图与待办

### 已在近期版本落地的能力（备忘）

| 主题 | 说明 |
|------|------|
| 扫描根可配 | 页眉路径 +「应用并扫描」、`POST /api/set-scan-root` |
| 退出 | 页眉「退出应用」、`POST /api/shutdown` |
| 扫描性能与护盘 | `MB_SCAN_*`、`MB_THUMB_COUNT`、`MB_DISK_PROFILE`、单次 ffprobe、软链与兜底枚举等 |
| 删除与缓存 | 删文件后同步删对应缩略图目录 |
| 扩展与路径 | `.mts` 等；粘贴路径规范化 |
| 画廊播放增强 | 键盘快进/快退/逐帧、音量、全屏、静音、图片缩放拖拽 |
| 视频技术信息 | 画廊内显示分辨率、编码（H264/HEVC 等）、码率、帧率 |
| 审阅交互优化 | 画廊内直接切换保留/待审；修复卡片「标为待审」按钮失效 |

### 仍可排的 backlog（约定后再做）

**P1 体验与安全**：删除前可选二次确认；删除进废纸篓；转码更细进度；默认仅监听 `127.0.0.1` 等。

**P2 功能**：搜索增强、审阅标签导出/导入、非 macOS 打开目录、窄屏侧栏、macOS 原生浏览文件夹选根目录。

**P3 无障碍与 i18n**：键盘导航网格、焦点环、中英切换。

**P4 分发**：公证签名、ffmpeg 体积优化等。

---

## 注意事项

- 画廊内删除**无浏览器二次确认**，请谨慎或先备份。  
- 转码占 CPU。  
- 「在文件管理器中打开」已支持 macOS / Windows / Linux。  
