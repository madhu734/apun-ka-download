FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install FFmpeg, curl, unzip, nodejs, and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies first
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Rust BotGuard PO Token Provider (bgutil-pot) & its official yt-dlp plugin into site-packages
RUN ARCH="$(uname -m)" && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        BIN_NAME="bgutil-pot-linux-aarch64"; \
    else \
        BIN_NAME="bgutil-pot-linux-x86_64"; \
    fi && \
    curl -fsSL "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/${BIN_NAME}" -o /usr/local/bin/bgutil-pot && \
    chmod +x /usr/local/bin/bgutil-pot && \
    curl -fsSL "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-ytdlp-pot-provider-rs.zip" -o /tmp/pot-plugin.zip && \
    mkdir -p /tmp/pot-plugin /etc/yt-dlp/plugins && \
    cp /tmp/pot-plugin.zip /etc/yt-dlp/plugins/bgutil-ytdlp-pot-provider-rs.zip && \
    unzip -q /tmp/pot-plugin.zip -d /tmp/pot-plugin && \
    SITE_PKG="$(python3 -c 'import site; print(site.getsitepackages()[0])')" && \
    if [ -d "/tmp/pot-plugin/yt_dlp_plugins" ]; then \
        cp -r /tmp/pot-plugin/yt_dlp_plugins "${SITE_PKG}/"; \
    elif [ -d "/tmp/pot-plugin/bgutil-ytdlp-pot-provider/yt_dlp_plugins" ]; then \
        cp -r /tmp/pot-plugin/bgutil-ytdlp-pot-provider/yt_dlp_plugins "${SITE_PKG}/"; \
    fi && \
    rm -rf /tmp/pot-plugin /tmp/pot-plugin.zip

# Copy application files
COPY main.py .
COPY static ./static

# Create directories for temporary downloads and cookie storage
RUN mkdir -p /app/temp_downloads /app/cookies

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "120"]
