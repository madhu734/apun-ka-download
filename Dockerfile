FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=denoland/deno:bin-2.9.6 /deno /usr/local/bin/deno

# Install FFmpeg and tools for fetching the PO Token provider
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Install the matching Rust BotGuard server and yt-dlp plugin
RUN ARCH="$(uname -m)" && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        BIN_NAME="bgutil-pot-linux-aarch64"; \
    else \
        BIN_NAME="bgutil-pot-linux-x86_64"; \
    fi && \
    RELEASE="https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/v0.8.1" && \
    curl -fsSL "${RELEASE}/${BIN_NAME}" -o /usr/local/bin/bgutil-pot && \
    chmod +x /usr/local/bin/bgutil-pot && \
    curl -fsSL "${RELEASE}/bgutil-ytdlp-pot-provider-rs.zip" -o /tmp/bgutil-plugin.zip && \
    unzip -q /tmp/bgutil-plugin.zip -d /tmp/bgutil-plugin && \
    PLUGIN_PACKAGE="$(find /tmp/bgutil-plugin -type d -name yt_dlp_plugins -print -quit)" && \
    test -n "${PLUGIN_PACKAGE}" && \
    cp -R "${PLUGIN_PACKAGE}" "$(python -c "import sysconfig; print(sysconfig.get_path('purelib'))")/" && \
    rm -rf /tmp/bgutil-plugin /tmp/bgutil-plugin.zip

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
