# 发行包目录（`package/`）

本目录用于存放**本地或 CI 构建**的安装介质；大文件已列入 `.gitignore`，**不会提交到 Git**。

## v1.2.3 预置包说明

- **macOS ARM**：本机执行 `./packaging/build_dmg.sh` 后，将 `dist/Media Browser v1.2.3.dmg` 复制到本目录。
- **Windows**：在 Windows 上执行 `packaging/build_windows.ps1`，或推送标签 `v1.2.3` 后从 GitHub Actions `release.yml` 的 **windows** artifact 下载 `MediaBrowser-v1.2.3-windows.zip` 复制到本目录。
- **Linux / Docker 离线包**：见下文 `docker build` + `docker save` 示例（文件名中的 `linux-$(uname -m)` 与架构一致）。


| 文件 | 说明 |
|------|------|
| `Media Browser v*.dmg` | macOS ARM（Apple Silicon）用 `packaging/build_dmg.sh` 在本机生成后复制到此处 |
| `MediaBrowser-v*-windows.zip` | Windows：在 Windows 上运行 `packaging/build_windows.ps1`，或依赖 GitHub Actions `release.yml` 产物 |
| `media-browser-*.tar.gz` | Docker：`docker build` + `docker save … \| gzip`（需能访问 Docker Hub；网络失败时换环境构建后复制到此处） |


## Docker 镜像包（示例）

```bash
docker build -t media-browser:1.2.3 .
docker save media-browser:1.2.3 | gzip -1 > package/media-browser-1.2.3-linux-$(uname -m).tar.gz
```

版本号与 `media_browser.py` 中 `APP_VERSION` 保持一致。

## QA（Playwright 画廊 E2E）

```bash
pip install -r requirements-dev.txt
playwright install chromium
pytest tests/test_gallery_e2e.py -v          # 仅画廊 E2E
pytest tests/ -q -m 'not e2e'               # 其余自动化（不含浏览器）
pytest tests/ -q                            # 全量（含 E2E）
```

## GitHub Release

打 `v*` 标签并 `git push origin v*` 后，`.github/workflows/release.yml` 会构建 macOS / Windows 并上传 Release 资源。
