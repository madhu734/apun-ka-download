# Apun Ka Download

Fast, minimalist media stream extractor for **YouTube**, **YouTube Music**, **Facebook**, **Instagram**, and **TikTok** powered by **FastAPI**, **FFmpeg**, and **yt-dlp**.

## Live Deployment
- **Live Application URL**: [http://iruropwov7dpshz81pxprsxf.140.238.160.99.sslip.io](http://iruropwov7dpshz81pxprsxf.140.238.160.99.sslip.io)
- **Admin Cookie Manager**: [http://iruropwov7dpshz81pxprsxf.140.238.160.99.sslip.io/admin](http://iruropwov7dpshz81pxprsxf.140.238.160.99.sslip.io/admin)
- **Health Check Endpoint**: [http://iruropwov7dpshz81pxprsxf.140.238.160.99.sslip.io/api/health](http://iruropwov7dpshz81pxprsxf.140.238.160.99.sslip.io/api/health)

## Key Features
- **Platform Auto-Detection**: Instant detection for YouTube, YT Music, Facebook, Instagram, and TikTok links.
- **Smart YT Music Override**: Automatically switches to Studio Audio mode (`.m4a` lossless stream or `320kbps .mp3` with embedded album art & metadata).
- **Real-Time Kinetic Sync**: Live `[ wait, processing ]` kinetic wave animation synced with backend `yt-dlp` & `FFmpeg` stages.
- **Zero-Config GitHub → Coolify Auto-Deploy**: Every push to `main` triggers `.github/workflows/deploy.yml` (`POST /api/webhook/github`) and is backed by an internal 60-second Git SHA watcher that automatically queues a fresh Coolify deployment.
