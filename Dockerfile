FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install FFmpeg, curl, unzip, nodejs, and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Rust BotGuard PO Token Provider (bgutil-pot) & yt-dlp plugin for aarch64/x86_64
RUN ARCH="$(uname -m)" && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        BIN_NAME="bgutil-pot-linux-aarch64"; \
    else \
        BIN_NAME="bgutil-pot-linux-x86_64"; \
    fi && \
    curl -fsSL "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/${BIN_NAME}" -o /usr/local/bin/bgutil-pot && \
    chmod +x /usr/local/bin/bgutil-pot && \
    mkdir -p /etc/yt-dlp/plugins/bgutil-ytdlp-pot-provider /root/.config/yt-dlp/plugins/bgutil-ytdlp-pot-provider && \
    curl -fsSL "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-ytdlp-pot-provider-rs.zip" -o /tmp/pot-plugin.zip && \
    unzip -q /tmp/pot-plugin.zip -d /etc/yt-dlp/plugins/bgutil-ytdlp-pot-provider && \
    unzip -q /tmp/pot-plugin.zip -d /root/.config/yt-dlp/plugins/bgutil-ytdlp-pot-provider && \
    rm -f /tmp/pot-plugin.zip

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY main.py .
COPY static ./static

# Create directories for temporary downloads and cookie storage
RUN mkdir -p /app/temp_downloads /app/cookies

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "120"]
