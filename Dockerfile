# Stage 1: Build official Brainicism bgutil-ytdlp-pot-provider server with Node 22
FROM node:22-slim AS bgutil-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates python3 make g++ && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil && \
    cd /opt/bgutil/server && \
    npm ci && \
    npx tsc

# Stage 2: Production Python 3.11 + FFmpeg + Node 22 + bgutil POT Server
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy Node 22 binary and compiled bgutil POT server/plugin from builder stage
COPY --from=bgutil-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=bgutil-builder /opt/bgutil /opt/bgutil

# Install FFmpeg, curl, and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies + matching bgutil plugin from /opt/bgutil/plugin
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir /opt/bgutil/plugin && \
    SITE_PKG="$(python3 -c 'import site; print(site.getsitepackages()[0])')" && \
    rm -f "${SITE_PKG}"/yt_dlp_plugins/extractor/getpot_bgutil_script*.py

# Copy application files
COPY main.py .
COPY static ./static

# Create directories for temporary downloads and cookie storage
RUN mkdir -p /app/temp_downloads /app/cookies

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "120"]
