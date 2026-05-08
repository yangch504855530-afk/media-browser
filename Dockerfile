# Media Browser — Python + ffmpeg/ffprobe（与 media_browser.py 要求一致）
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY media_browser.py .

ENV MB_HOST=0.0.0.0
EXPOSE 8765

# -u 减少日志缓冲，便于在 NAS / Portainer 里看输出
CMD ["python", "-u", "media_browser.py"]
