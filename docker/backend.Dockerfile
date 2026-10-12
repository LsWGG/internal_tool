FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# geopandas/rasterio、Playwright Chromium 和 Mermaid CLI 所需的运行时依赖。
# ffmpeg 是采集器的视频下载组件要的：提取音频、嵌入字幕/封面、写入元数据都靠它，
# 它也一并提供 ffprobe（音频模式要用它读编解码信息）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git nodejs npm ffmpeg \
       libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
       libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
       libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
       libfontconfig1 libfreetype6 libjpeg62-turbo libpng16-16 \
       libx11-6 libxcb1 libxext6 libxi6 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt \
    && python -m playwright install chromium \
    && npm install --global @mermaid-js/mermaid-cli \
    && rm -rf /root/.cache/pip

COPY backend /app/backend

WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "run.py"]
