# Media Browser — Python + ffmpeg/ffprobe（与 media_browser.py 要求一致）
# 发版时请与仓库内 APP_VERSION（当前 v1.3.0）对齐；镜像 tag 建议 media-browser:1.3.0
FROM python:3.12-slim-bookworm

# NAS / 国内网络构建时 apt 拉 deb.debian.org 易超时，导致「Unable to locate package ffmpeg」。
# docker compose 默认传 mirrors.aliyun.com；海外直连可：docker compose build --build-arg APT_MIRROR=
ARG APT_MIRROR=mirrors.aliyun.com
ARG APP_VERSION=1.3.0
LABEL org.opencontainers.image.title="Media Browser" \
      org.opencontainers.image.version="${APP_VERSION}"

RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        [ -f "$f" ] || continue; \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" "$f"; \
      done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg intel-media-va-driver libva-drm2; \
    rm -rf /var/lib/apt/lists/*

# 创建非 root 用户（uid=1000），避免缩略图在宿主机上生成 root 权限文件
RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -m -s /bin/bash appuser

WORKDIR /app
COPY media_browser.py .
RUN chown -R appuser:appgroup /app

USER appuser

ENV MB_HOST=0.0.0.0
EXPOSE 8765

# -u 减少日志缓冲，便于在 NAS / Portainer 里看输出
CMD ["python", "-u", "media_browser.py"]
