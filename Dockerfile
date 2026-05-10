# Media Browser — Python + ffmpeg/ffprobe（与 media_browser.py 要求一致）
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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
