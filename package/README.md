# 发行包目录（`package/`）

本目录用于存放**本地或 CI 构建**的安装介质；大文件已列入 `.gitignore`，**不会提交到 Git**。

| 文件 | 说明 |
|------|------|
| `Media Browser v*.dmg` | macOS ARM（Apple Silicon）用 `packaging/build_dmg.sh` 在本机生成后复制到此处 |
| `MediaBrowser-v*-windows.zip` | Windows：在 Windows 上运行 `packaging/build_windows.ps1`，或依赖 GitHub Actions `release.yml` 产物 |
| `media-browser-*.tar.gz` | Docker：`docker build` + `docker save … \| gzip`（需能访问 Docker Hub；网络失败时换环境构建后复制到此处） |


## Docker 镜像包（示例）

```bash
docker build -t media-browser:1.2.0 .
docker save media-browser:1.2.0 | gzip -1 > package/media-browser-1.2.0-linux-$(uname -m).tar.gz
```

版本号与 `media_browser.py` 中 `APP_VERSION` 保持一致。

## GitHub Release

打 `v*` 标签并 `git push origin v*` 后，`.github/workflows/release.yml` 会构建 macOS / Windows 并上传 Release 资源。
