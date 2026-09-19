# Media Browser 故障排查清单

面向 **Python 单文件服务**、`ffmpeg`/`ffprobe`、可选 **Docker** 与 **Ollama** 部署。建议先查 **`/health`**（JSON）与 **stderr 日志**（`MB_LOG_FORMAT=json` 便于接入采集）。

---

## 1. 打不开页面（浏览器无法访问）

| 检查项 | 说明 |
|--------|------|
| 进程是否在跑 | 终端是否仍在跑 `python3 media_browser.py`；Docker 用 `docker compose ps` / `docker ps`。 |
| 端口与地址 | 默认 `8765`；`MB_HOST=0.0.0.0` 才可被局域网访问，仅本机试 `http://127.0.0.1:8765/`。 |
| 防火墙 / 安全组 | 云主机或 NAS 是否放行 TCP 端口。 |
| 容器端口映射 | `docker-compose.yml` 中 `ports: "8765:8765"` 是否与宿主机冲突。 |
| 本机探测 | `curl -sS http://127.0.0.1:8765/health` 应返回 JSON；若连接被拒绝说明服务未监听或端口错误。 |
| 日志 | 启动失败时看 stderr：`MB_LOG_LEVEL=DEBUG` 可加重现信息。 |

---

## 2. 扫描不到新文件 / 列表不更新

| 检查项 | 说明 |
|--------|------|
| **作品规则** | 只索引**扫描根下的一级子文件夹**内（任意深度）的媒体；根目录平铺文件会合并为「根目录内的媒体…」一条作品。 |
| 扩展名 | 须为内置 `VIDEO_EXTS` / `IMAGE_EXTS`（见 `media_browser.py`）；非常见后缀需改代码加入。 |
| 页眉「应用并扫描」 | 改 `MB_ROOT_DIR` 或页眉路径后必须点 **应用并扫描** 才会重置枚举。 |
| 扫描未完成 | `/api/progress` 或 `/health` 中 `scan.state` 为 `scanning` 时列表会逐步出现；大库+慢盘请耐心等待。 |
| 枚举错误 | `/health` → `scan.enum_error` 或 `scan.state=error`；常见为**无读权限**、路径不存在、外置盘未挂载。 |
| `.Trash` | 名为 `.Trash` / `.Trashes` 的目录会被跳过（设计行为）。 |

### 2.1 macOS：⌘K 挂载 NAS 后「路径不存在或不是文件夹」

| 检查项 | 说明 |
|--------|------|
| 路径格式 | 须填 **本机路径** `/Volumes/共享名/子文件夹`，**不要**填 `smb://10.x.x.x/...`（v1.2.4+ 会明确提示）。 |
| 是否已挂载 | Finder 侧边栏能看到磁盘；终端 `ls /Volumes` 应列出卷名（如 `handle-3T-cun3`）。 |
| 示例 | 共享内文件夹 `tmp` → `/Volumes/handle-3T-cun3/tmp`（以你机器 `ls /Volumes` 为准）。 |
| 拷贝路径 | 在 Finder 对目标文件夹 **⌥+右键 → 将“…”拷贝为路径名称**，粘贴到页眉「扫描根目录」。 |
| 唤醒挂载 | `open "/Volumes/你的共享名"` 后再点「应用并扫描」。 |
| 版本 | NAS 路径解析增强自 **v1.2.4**；旧版 .app 请升级 Release 或 `git pull` 后用脚本运行。 |

---

## 3. 缩略图不显示 / 裂图

| 检查项 | 说明 |
|--------|------|
| **ffmpeg / ffprobe** | `/health` 中 `ffmpeg.available`、`ffprobe.available` 须为真；容器内需把二进制打进镜像或保证 PATH。 |
| 缓存目录 | `MB_CACHE_DIR` 可写、磁盘未满（`/health` → `disk.used_percent`）。 |
| 权限 | 容器 `user` UID/GID 是否对挂载的 `MB_CACHE_DIR`、`MB_ROOT_DIR` 可读可写。 |
| 源文件路径 | `/file`、`/thumb/...` 受 **必须在扫描根下** 限制；越权会 403/404。 |
| 清缓存 | 可删 `MB_CACHE_DIR` 下对应哈希子目录或整个缓存目录后重扫（会重新生成缩略图）。 |

### 3.1 Docker 权限错误码（删除 / 恢复 / 清空回收站）

API 返回 `403` 时，先看响应里的 `code`，不要直接放开挂载写权限或移除确认头：

| 错误码 | 常见原因 | 排查与处理 |
|--------|----------|------------|
| `MEDIA_READONLY` | 显式设置了 `MB_MEDIA_READONLY=1`，或应用探测判定媒体根只读 | 若确需审阅删除/恢复，去掉该开关或改用可写 bind mount；NAS 源盘保持只读时应继续保留保护。 |
| `SOURCE_PARENT_READ_ONLY` | 容器用户对源文件父目录没有写权限（POSIX 常见 `EPERM`），无法安全移入回收站 | 检查 bind mount 的宿主属主和容器 UID/GID；对需要审阅移动的父目录授予写权限，缓存目录保持可写。 |
| `PATH_OUTSIDE_ROOT` | 请求路径不是扫描根下的常规媒体路径，或包含越界/符号链接逃逸 | 核对页面显示路径与挂载位置；只对 `MB_ROOT_DIR` 下的路径操作，不要手动构造根外路径。 |
| `PURGE_REQUIRES_CONFIRM` | 清空回收站缺少显式 `PURGE` 确认 | 这是防误清保护；确认要永久清理时按接口要求同时传确认 body 和 `X-MB-Recycle-Confirm: PURGE`。清空前先确认回收站内容已不再需要。 |

---

## 4. AI 标签不生成 / 分析失败

| 检查项 | 说明 |
|--------|------|
| **Ollama 服务** | 本机执行 `ollama serve`；`/health` → `ollama.reachable` 为 `true`。 |
| 模型已拉取 | `ollama pull llava`（或 `MB_OLLAMA_MODEL` 指定模型）。 |
| 环境变量 | `MB_OLLAMA_HOST`（默认 `http://127.0.0.1:11434`）、`MB_OLLAMA_TIMEOUT`、`MB_ANALYZE_FRAME_COUNT`。 |
| 容器网络 | 若 Ollama 在宿主机、应用在容器，**localhost 指向容器自身**；需改为宿主机网关或 `host.docker.internal`（视平台而定）。 |
| 浏览器任务页 | 创建任务 → **AI 分析** → 看进度与错误文案；服务端日志级别设为 `DEBUG` 可查请求细节。 |

---

## 5. 容器反复重启（Docker）

| 检查项 | 说明 |
|--------|------|
| **`docker compose logs -f`** | 看 Python  traceback 或「Address already in use」。 |
| **healthcheck** | `docker-compose.yml` 中 `healthcheck` 访问 `http://127.0.0.1:8765/health`：若持续 **503**（如 ffmpeg 不可用、磁盘满、枚举错误），编排可能将容器标为 **unhealthy**；需对照 `/health` JSON 修根因。 |
| 卷挂载 | 扫描根、缓存路径若挂载错或不存在，进程可能异常退出。 |
| `restart: unless-stopped` | 会无限重启；先 `docker compose run --rm ... sh` 或临时去掉 restart 便于调试。 |
| 资源 | 内存不足 OOM；大库首次扫描 CPU 飙高属正常，可适当降低 `MB_SCAN_WORKERS` / `MB_THUMB_COUNT`。 |

---

## 相关端点与变量速查

- **健康检查**：`GET /health`（异常时 **503** + `"ok": false`）。
- **日志**：`MB_LOG_LEVEL`、`MB_LOG_FORMAT=json`。
- **扫描**：`MB_ROOT_DIR`、`MB_CACHE_DIR`、`MB_DISK_PROFILE`、`MB_SCAN_WORKERS`。
