# Media Browser 架构评估（单文件 `media_browser.py`）

> 统计基于仓库 v1.4.2（约 **7700 行**，其中大部分为内嵌前端模板）。
> 指标由 `ast` 解析脚本生成，用于相对评估而非绝对审计。

---

## 1. 现状扫描

### 1.1 量化指标（`media_browser.py`）

| 指标 | 数值 | 说明 |
|------|------|------|
| **总行数** | ~7700 | 含巨型内嵌 `HTML_PAGE` 模板 |
| **顶层函数** | 持续增长 | 模块 `body` 内 `def` |
| **函数定义节点（含类内/嵌套）** | **137+** | `ast.walk` 中全部 `FunctionDef` |
| **类** | **6** | `_JsonLogFormatter`、`_ColorTextFormatter`、`MediaScanner`、`AnalysisTaskManager`、`Handler`、`ThreadedHTTPServer` |
| **模块级赋值名** | **26**（其中约 **19** 为非双下划线/可视为「配置与全局单例」） | 含 `ROOT_DIR`、`scanner`、`logger` 等 |
| **单函数最大行数** | **250+** | `Handler.do_POST` |
| **长函数热点** | `do_POST`、`do_GET`、内嵌前端函数 | HTTP、前端与 AI 路径集中 |

**内嵌前端**：`HTML_PAGE` 自约 **2092** 行起至文件末尾前，约 **2500+ 行** 的 HTML/CSS/JS 与 Python 同文件，是行数膨胀的主因。

### 1.2 天然边界（逻辑分层）

| 区域 | 大致内容 | 耦合点 |
|------|-----------|--------|
| **配置 / 常量** | 环境变量、`VIDEO_EXTS`、`ROOT_DIR`、`setup_logging`、`FFMPEG_BIN` | 全局在 import 时初始化 |
| **路径与安全** | `get_scan_root`、`is_path_under_root`、`replace_scan_root` | 与 `scanner` 全局单例强绑定 |
| **缩略图 / 媒体工具** | `generate_*_thumb`、`get_video_info`、`ffprobe` 封装 | 依赖 `CACHE_DIR`、`THUMB_WIDTH` |
| **扫描业务** | `MediaScanner`、枚举、`_process_work` | 依赖上两项 + `allWorks` 经 HTTP 吐出 |
| **AI 标签 / 任务** | `AnalysisTaskManager`、Ollama、`_normalize_llm_insight` | 独立度较高，但仍经 `Handler` 暴露 |
| **HTTP 路由层** | `Handler.do_GET` / `do_POST`、`_send_json`、静态文件 | 巨型分支表 |
| **运维** | `build_health_payload`、日志 | 已相对可抽离 |
| **前端模板** | `HTML_PAGE` + 内联 JS | 与 API 路径字符串硬编码在同一文件 |

### 1.3 维护成本（经验性）

新增一项典型能力时，**常见触及面**：

1. **路由**：`do_GET` / `do_POST` 各 **1 处**（+ 偶尔 `OPTIONS`）。
2. **前端**：`HTML_PAGE` 内 **JS/HTML 若干处**（按钮、fetch URL、`switchTab` 等）。
3. **业务**：扫描 / AI / 工具函数中 **0～2 个** 模块区。
4. **文档**：`README.md` 的 HTTP 表 / 环境变量表 **约 1 处**。

**粗估**：平均 **3～6 处** 同一文件内修改；若含 UI，**单次 PR  diff 容易上千行**（心理成本高、review 慢）。

---

## 2. 拆分方案（最小改动原则）

### 2.1 Phase 1 建议先拆出的模块

优先级按 **依赖少、对行为零改变、行数收益明显** 排序：

| 顺序 | 新模块（示例路径） | 内容 | 收益 |
|------|---------------------|------|------|
| 1 | `mb_pkg/logging_config.py` | `_JsonLogFormatter`、`_ColorTextFormatter`、`setup_logging` | 减小头部噪音，便于测日志格式 |
| 2 | `mb_pkg/health.py` | `_tool_version_ok`、`_cache_dir_stats`、`build_health_payload` | `/health` 与扫描核心解耦 |
| 3 | `mb_pkg/constants.py` | `VIDEO_EXTS`、`IMAGE_EXTS`、`APP_VERSION`、扩展名集合 | 单测可只 import 常量 |
| 4 | （可选）`mb_pkg/config.py` | 环境变量读取；**注意**：大量全局在 import 时赋值，需 **显式 `load_config()`** 或保持「import 副作用」与现网一致 | 改动面大，建议放 Phase 2 |

**不建议 Phase 1 动**：`Handler`、`HTML_PAGE`、`MediaScanner` 主体——一动牵涉全局初始化顺序与 Docker 入口习惯。

### 2.2 向后兼容约束

- **Docker / 本地启动**：继续 **`python3 media_browser.py`**（根目录保留同名入口文件）。
- **环境变量 / HTTP**：不增删改路径语义。
- **实现方式**：根目录 `media_browser.py` 保留为 **薄入口**（`from mb_pkg.health import build_health_payload` 等），或保持单文件仅 **把子模块当「内部库」** 由同一 repo 引用——二者择一，避免与标准库 `http` 命名冲突即可（包名勿叫 `http`）。

### 2.3 目录结构（Phase 1 示意）

```
media-browser/
  media_browser.py          # 入口：main + Handler + HTML_PAGE（暂留）
  mb_pkg/
    __init__.py             # 可空；或 re-export 供测试
    logging_config.py
    health.py
    constants.py
  tests/
  Dockerfile                # CMD 不变
```

### 2.4 import 调整计划（PR 级步骤）

1. 新建 `mb_pkg/`，移入 **无循环依赖** 的纯函数/类（日志、health、常量）。
2. 根 `media_browser.py` 顶部增加 `from mb_pkg.health import build_health_payload` 等，**删除**原文件内重复定义。
3. **全量** `pytest tests/` + 手动 `curl /health`。
4. README 增加一行：「核心逻辑逐步迁至 `mb_pkg/`（演进中）」。

---

## 3. 不拆分方案（维持单文件时的健康规范）

若 **6～12 个月**内仍以单文件为主，建议团队约定：

| 规范 | 建议值 |
|------|--------|
| **单函数行数** | 新增代码 **≤ 80 行**；已有超长函数（如 `do_GET`）以 **「提取 `_route_*` 私有函数」** 方式渐进收缩 |
| **区域分隔** | 沿用并强化 `# ===================== 标题 =====================` 横幅；禁止在无横幅区间插入大块新逻辑 |
| **常量** | 新扩展名/魔数一律放在 **`VIDEO_EXTS` 邻域** 或单独 `# ---- 常量区 ----`，禁止散落在 Handler 内 |
| **路由** | 新端点优先写成 **`Handler` 上的 `_handle_foo(self, parsed)`**，由 `do_GET` 一行分发，避免继续堆叠 `elif` |
| **前端** | 任何非微小 UI 变更，**强制**在 PR 描述中附「影响的 API 列表」；长期目标仍为 **模板外置**（`templates/` + 构建或运行时读文件） |

---

## 4. 结论与建议

### 4.1 建议：**短期维持单文件 + 规范收紧；中期执行 Phase 1 物理拆分**

| 维度 | 判断 |
|------|------|
| **团队规模 / 发布频率** | 若 1～2 人、低频发版，单文件 **可接受**；行数主要来自 **模板**，非 Python 复杂度失控。 |
| **风险** | 一次性大拆 **Handler + 全局初始化** 易引入回归；与「零依赖单文件」心智冲突。 |
| **收益** | Phase 1 拆 **logging + health + constants** 成本低、**测试与运维可读性**立刻提升，且不触动 Docker 语义。 |

**一句话**：不必为「4624 行」而拆；要为 **「路由/模板/配置的可测试边界」** 而拆——**先做最小 `mb_pkg` PR，再视情况把 AI 与扫描迁出**。

### 4.2 若采纳拆分：可立即执行的 PR（1 个 Sprint 内）

1. **PR 标题**：`refactor: extract mb_pkg (logging, health, constants)`  
2. **范围**：仅移动代码 + import，无行为变更。  
3. **验收**：`pytest` 全绿；`curl /health` 字段与状态码与拆分前一致；Dockerfile **CMD 不变**。  
4. **回滚**：单 commit revert 即可。

---

## 附录：统计命令（可复现）

```bash
python3 - <<'PY'
import ast
from pathlib import Path
tree = ast.parse(Path("media_browser.py").read_text(encoding="utf-8"))
funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
mx = max((getattr(n,"end_lineno",n.lineno)-n.lineno+1) for n in funcs)
print(len(funcs), "functions,", len(classes), "classes,", mx, "max func lines")
PY
```
