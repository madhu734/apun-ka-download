FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install FFmpeg, curl, unzip, nodejs, and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Rust BotGuard PO Token Server binary (bgutil-pot)
RUN ARCH="$(uname -m)" && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        BIN_NAME="bgutil-pot-linux-aarch64"; \
    else \
        BIN_NAME="bgutil-pot-linux-x86_64"; \
    fi && \
    curl -fsSL "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/${BIN_NAME}" -o /usr/local/bin/bgutil-pot && \
    chmod +x /usr/local/bin/bgutil-pot

# Install Python dependencies and remove buggy script-deno POT provider so HTTP POT provider (127.0.0.1:4416) is used cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    SITE_PKG="$(python3 -c 'import site; print(site.getsitepackages()[0])')" && \
    rm -f "${SITE_PKG}"/yt_dlp_plugins/extractor/getpot_bgutil_script*.py

# Copy application files
COPY main.py .
COPY static ./static

# Create directories for temporary downloads and cookie storage
RUN mkdir -p /app/temp_downloads /app/cookies

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "120"]
