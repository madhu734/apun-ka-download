# Apun Ka Download

Fast, minimalist media stream extractor for **YouTube**, **YouTube Music**, **Facebook**, **Instagram**, and **TikTok** powered by **FastAPI**, **FFmpeg**, and **yt-dlp**.

## Live Deployment
- **Primary HTTPS URL**: [https://download.tmadhu.me](https://download.tmadhu.me)
- **Admin Cookie Manager**: [https://download.tmadhu.me/admin](https://download.tmadhu.me/admin)
- **Health Check Endpoint**: [https://download.tmadhu.me/api/health](https://download.tmadhu.me/api/health)

## Features
- **Platform Auto-Detection**: Instant detection for YouTube, YT Music, Facebook, Instagram, and TikTok links.
- **Smart YT Music Override**: Automatically switches to Studio Audio mode (`.m4a` lossless stream or `320kbps .mp3` with embedded album art & metadata).
- **Real-Time Kinetic Sync**: Live `[ wait, processing ]` kinetic wave animation synced with backend `yt-dlp` & `FFmpeg` stages.
- **Direct SSL & Unlimited Connection Duration**: Served directly via Coolify's Traefik proxy with Let's Encrypt SSL (`readTimeout=0s`, `writeTimeout=0s`, `idleTimeout=0s`) and extended keep-alive so large downloads/uploads never time out.
- **Auto-Deploy Pipeline**: Every push to `main` triggers `.github/workflows/deploy.yml` -> `/api/webhook/github` (plus a built-in 60s GitHub commit watcher fallback) to redeploy automatically on Coolify.
