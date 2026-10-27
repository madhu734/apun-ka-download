# Apun Ka Download

Fast, minimalist media stream extractor for **YouTube**, **YouTube Music**, **Facebook**, **Instagram**, and **TikTok** powered by **FastAPI**, **FFmpeg**, and **yt-dlp**.

## 🌐 Live Production Deployment
- **Live App URL**: [https://download.tmadhu.me](https://download.tmadhu.me)
- **Cookie Manager (No Password Required)**: [https://download.tmadhu.me/admin](https://download.tmadhu.me/admin)
- **Health Check Endpoint**: [https://download.tmadhu.me/api/health](https://download.tmadhu.me/api/health)

## ⚡ High-Speed Unlimited Connections
- **Direct Traefik Reverse Proxy**: Configured via Cloudflare DNS-Only (`proxied: false`) with genuine Let's Encrypt SSL.
- **Zero Timeouts**: Built with `readTimeout=0s`, `writeTimeout=0s`, `idleTimeout=0s` for uninterrupted, large audio/video downloads and streams.

## 🍪 Password-Free Cookie Manager (/admin)
When YouTube or other platforms enforce bot-detection or require sign-in for age-restricted / premium streams:
1. Export your cookies using the **Get cookies.txt LOCALLY** Chrome/Edge extension.
2. Open [https://download.tmadhu.me/admin](https://download.tmadhu.me/admin).
3. Drop the `cookies.txt` file or paste the text and click **Save & Activate Cookies**.
4. No password or PIN required — anyone can update expired cookies instantly.

## 🚀 Auto-Deploy Pipeline
- Repository Webhook linked directly to Coolify.
- Every push to `main` branch automatically triggers Coolify to pull and redeploy the latest container build.
