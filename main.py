import os
import re
import json
import time
import uuid
import hmac
import hashlib
import shutil
import asyncio
import logging
import contextlib
from pathlib import Path
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("apun_ka_download")

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp_downloads"
COOKIES_FILE = BASE_DIR / "cookies" / "cookies.txt"

TEMP_DIR.mkdir(exist_ok=True)
(BASE_DIR / "cookies").mkdir(exist_ok=True)

# Environment config for Auto-Deploy, Domain Mapping & Admin Cookie Manager
GITHUB_REPO = os.environ.get("GITHUB_REPO", "madhu734/apun-ka-download").strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
COOLIFY_BASE_URL = os.environ.get("COOLIFY_BASE_URL", "http://140.238.160.99:8000/api/v1").strip()
COOLIFY_API_TOKEN = os.environ.get("COOLIFY_API_TOKEN", "").strip()
COOLIFY_APP_UUID = os.environ.get("COOLIFY_APP_UUID", "iruropwov7dpshz81pxprsxf").strip()
TARGET_FQDN = os.environ.get("TARGET_FQDN", "https://download.tmadhu.me").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
ADMIN_PIN = os.environ.get("ADMIN_PIN", "123456").strip()

# In-memory job store backed by disk status files for multi-worker/process safety
JOBS: Dict[str, Dict[str, Any]] = {}
BOOT_COMMIT_SHA: Optional[str] = None
LAST_DEPLOY_TRIGGERED_SHA: Optional[str] = None


async def fetch_latest_github_sha() -> Optional[str]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ApunKaDownload-AutoSync/1.0",
    }
    gh_token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            res = await client.get(url, headers=headers)
            if res.status_code == 200:
                return res.json().get("sha")
    except Exception as exc:
        logger.debug(f"GitHub SHA check failed: {exc}")
    return None


async def trigger_coolify_redeploy(reason: str = "github_push") -> Dict[str, Any]:
    if not COOLIFY_API_TOKEN or not COOLIFY_APP_UUID:
        return {"triggered": False, "reason": "Coolify credentials not configured"}

    deploy_url = f"{COOLIFY_BASE_URL.rstrip('/')}/deploy"
    headers = {
        "Authorization": f"Bearer {COOLIFY_API_TOKEN}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            deploy_url,
            params={"uuid": COOLIFY_APP_UUID, "force": "false"},
            headers=headers,
        )
        logger.info(f"Coolify auto-deploy triggered ({reason}): HTTP {res.status_code}")
        return {
            "triggered": res.status_code < 400,
            "status_code": res.status_code,
            "reason": reason,
        }


async def ensure_coolify_domain_mapped():
    """Ensures Coolify has mapped TARGET_FQDN (https://download.tmadhu.me) with Let's Encrypt SSL."""
    if not COOLIFY_API_TOKEN or not COOLIFY_APP_UUID or not TARGET_FQDN:
        return
    base = COOLIFY_BASE_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {COOLIFY_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.get(f"{base}/applications/{COOLIFY_APP_UUID}", headers=headers)
            if res.status_code != 200:
                logger.warning(f"Could not inspect Coolify app domain: HTTP {res.status_code}")
                return
            app_data = res.json()
            current_fqdn = (app_data.get("fqdn") or "").strip()
            if current_fqdn != TARGET_FQDN:
                logger.info(f"Updating Coolify domain from '{current_fqdn}' to '{TARGET_FQDN}'...")
                patch_res = await client.patch(
                    f"{base}/applications/{COOLIFY_APP_UUID}",
                    headers=headers,
                    json={"domains": TARGET_FQDN},
                )
                logger.info(f"Domain update response: HTTP {patch_res.status_code} - {patch_res.text}")
                if patch_res.status_code < 400:
                    logger.info("Triggering Coolify redeploy to apply Traefik SSL & domain labels...")
                    await trigger_coolify_redeploy(reason="apply_ssl_domain")
            else:
                logger.info(f"Coolify domain is active and mapped to {TARGET_FQDN}")
    except Exception as exc:
        logger.error(f"Error ensuring Coolify domain mapping: {exc}")


async def github_auto_sync_watcher():
    """Background watcher that polls GitHub main branch SHA and triggers Coolify deploy on update."""
    global BOOT_COMMIT_SHA, LAST_DEPLOY_TRIGGERED_SHA
    await asyncio.sleep(3)
    await ensure_coolify_domain_mapped()
    BOOT_COMMIT_SHA = await fetch_latest_github_sha()
    logger.info(f"Initialized GitHub Auto-Sync Watcher for {GITHUB_REPO}@{GITHUB_BRANCH} (SHA: {BOOT_COMMIT_SHA})")

    while True:
        await asyncio.sleep(60)
        try:
            latest_sha = await fetch_latest_github_sha()
            if not latest_sha:
                continue
            if BOOT_COMMIT_SHA is None:
                BOOT_COMMIT_SHA = latest_sha
                continue
            if latest_sha != BOOT_COMMIT_SHA and latest_sha != LAST_DEPLOY_TRIGGERED_SHA:
                logger.info(f"New commit detected on {GITHUB_REPO}: {latest_sha[:7]} (was {BOOT_COMMIT_SHA[:7]})")
                LAST_DEPLOY_TRIGGERED_SHA = latest_sha
                await trigger_coolify_redeploy(reason=f"auto_watcher:{latest_sha[:7]}")
        except Exception as exc:
            logger.debug(f"Auto-sync watcher error: {exc}")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    watcher_task = asyncio.create_task(github_auto_sync_watcher())
    try:
        yield
    finally:
        watcher_task.cancel()


app = FastAPI(title="Apun Ka Download API", version="1.3.0", lifespan=lifespan)


class AnalyzeRequest(BaseModel):
    url: str
    mode: str = "video"  # "video" or "audio"


class PrepareRequest(BaseModel):
    url: str
    preset: str  # "high", "medium", "low", "m4a", "mp3"
    platform: str


class CookieSaveRequest(BaseModel):
    pin: str
    cookies_content: str


def save_job_state(job_id: str, data: Dict[str, Any]):
    JOBS[job_id] = data
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    state_file = job_dir / "status.json"
    try:
        state_file.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def load_job_state(job_id: str) -> Optional[Dict[str, Any]]:
    if job_id in JOBS:
        return JOBS[job_id]
    state_file = TEMP_DIR / job_id / "status.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            JOBS[job_id] = data
            return data
        except Exception:
            return None
    return None


def detect_platform(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url.strip())
        host = parsed.netloc.lower()
    except Exception:
        return None

    if "music.youtube.com" in host:
        return "ytmusic"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "facebook.com" in host or "fb.watch" in host or "fb.gg" in host:
        return "facebook"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    return None


def is_facebook_group_url(url: str) -> bool:
    return bool(re.search(r"facebook\.com/groups/[^/]+", url, re.IGNORECASE))


def format_bytes(size: Optional[float]) -> str:
    if not size or size <= 0:
        return "Auto Size"
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def get_base_ydl_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        },
    }
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def cleanup_old_jobs():
    """Remove temporary folders older than 15 minutes."""
    now = time.time()
    expired_ids = [
        jid for jid, data in list(JOBS.items())
        if now - data.get("created_at", now) > 900
    ]
    for jid in expired_ids:
        job_dir = TEMP_DIR / jid
        shutil.rmtree(job_dir, ignore_errors=True)
        JOBS.pop(jid, None)

    for folder in TEMP_DIR.iterdir():
        if folder.is_dir():
            try:
                if now - folder.stat().st_mtime > 900:
                    shutil.rmtree(folder, ignore_errors=True)
                    JOBS.pop(folder.name, None)
            except Exception:
                pass


def build_video_options(info: Dict[str, Any]) -> List[Dict[str, str]]:
    formats = info.get("formats") or []
    duration = info.get("duration") or 0

    heights = set()
    size_by_height: Dict[int, float] = {}

    for f in formats:
        vcodec = f.get("vcodec")
        if not vcodec or vcodec == "none":
            continue
        w = f.get("width") or 0
        h = f.get("height") or 0
        short_edge = min(w, h) if (w and h) else (h or w)
        if short_edge > 0:
            heights.add(short_edge)
            fsize = f.get("filesize") or f.get("filesize_approx") or 0
            if not fsize and f.get("tbr") and duration:
                fsize = (f["tbr"] * 1000 / 8) * duration
            if fsize > size_by_height.get(short_edge, 0):
                size_by_height[short_edge] = fsize

    sorted_h = sorted(heights)
    if not sorted_h:
        return [
            {
                "id": "high",
                "label": "High / HD",
                "sublabel": "Best Available Quality (MP4)",
                "badge": "MAX HD",
                "size": "Optimized",
            }
        ]

    max_h = sorted_h[-1]
    med_candidates = [h for h in sorted_h if 540 <= h <= 780]
    low_candidates = [h for h in sorted_h if h < 540]

    options: List[Dict[str, str]] = []

    high_res_label = f"{max_h}p" if max_h >= 720 else f"{max_h}p Standard"
    options.append({
        "id": "high",
        "label": "High / HD",
        "sublabel": f"Original {high_res_label} Remux • MP4",
        "badge": f"{max_h}p HD" if max_h >= 720 else f"{max_h}p",
        "size": format_bytes(size_by_height.get(max_h)),
    })

    if med_candidates and max_h > 720:
        med_h = med_candidates[-1]
        options.append({
            "id": "medium",
            "label": "Medium Quality",
            "sublabel": f"Smart Balanced {med_h}p • Smaller Size",
            "badge": f"{med_h}p Smart",
            "size": format_bytes(size_by_height.get(med_h)),
        })
    elif len(sorted_h) >= 2 and max_h <= 720:
        med_h = sorted_h[len(sorted_h) // 2]
        if med_h != max_h:
            options.append({
                "id": "medium",
                "label": "Medium Quality",
                "sublabel": f"Balanced {med_h}p • MP4",
                "badge": f"{med_h}p",
                "size": format_bytes(size_by_height.get(med_h)),
            })

    if low_candidates:
        low_h = low_candidates[-1]
        if low_h != max_h:
            options.append({
                "id": "low",
                "label": "Low / Data Saver",
                "sublabel": f"Compact {low_h}p • Fast Download",
                "badge": f"{low_h}p Light",
                "size": format_bytes(size_by_height.get(low_h)),
            })

    return options


def build_audio_options(platform: str) -> List[Dict[str, str]]:
    if platform in ("youtube", "ytmusic"):
        return [
            {
                "id": "m4a",
                "label": "Original Studio Audio",
                "sublabel": "Untouched Master Stream (.m4a) + Album Art",
                "badge": "LOSSLESS STREAM",
                "size": "Original Bitrate",
            },
            {
                "id": "mp3",
                "label": "Converted MP3 Audio",
                "sublabel": "Universal 320kbps MP3 + Metadata & Cover",
                "badge": "320 KBPS MP3",
                "size": "High Quality",
            },
        ]
    else:
        return [
            {
                "id": "mp3",
                "label": "Download MP3 Audio",
                "sublabel": "Direct High-Quality MP3 Audio Track",
                "badge": "MP3 AUDIO",
                "size": "Direct Extract",
            }
        ]


@app.post("/api/analyze")
async def analyze_media(req: AnalyzeRequest):
    cleanup_old_jobs()
    url = req.url.strip()
    platform = detect_platform(url)

    if not platform:
        raise HTTPException(
            status_code=400,
            detail="Unsupported link. Please paste a valid YouTube, YT Music, Facebook, Instagram, or TikTok URL.",
        )

    effective_mode = "audio" if platform == "ytmusic" else req.mode

    def _extract():
        opts = get_base_ydl_opts()
        opts["extract_flat"] = False
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_extract)
    except Exception as exc:
        err_msg = str(exc).lower()
        if platform == "facebook" and (
            is_facebook_group_url(url)
            or "login" in err_msg
            or "private" in err_msg
            or "unavailable" in err_msg
            or "permission" in err_msg
        ):
            raise HTTPException(
                status_code=403,
                detail="This link belongs to a private Facebook group or restricted post. Only public Facebook videos can be downloaded.",
            )
        raise HTTPException(
            status_code=422,
            detail="Unable to fetch media from this link. Ensure the post is public and the URL is valid.",
        )

    if not info:
        raise HTTPException(status_code=404, detail="No media stream found at this URL.")

    if "entries" in info and info["entries"]:
        info = next((e for e in info["entries"] if e), None)
        if not info:
            raise HTTPException(status_code=404, detail="No playable media found.")

    options = (
        build_audio_options(platform)
        if effective_mode == "audio"
        else build_video_options(info)
    )

    return {
        "platform": platform,
        "effective_mode": effective_mode,
        "mode_overridden": (platform == "ytmusic" and req.mode == "video"),
        "title": info.get("title") or f"{platform.capitalize()} Media",
        "uploader": info.get("uploader") or info.get("channel") or info.get("artist") or platform.upper(),
        "duration": format_duration(info.get("duration")),
        "thumbnail": info.get("thumbnail"),
        "options": options,
    }


def run_download_job(job_id: str, url: str, preset: str, platform: str):
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def update_stage(stage_text: str, progress: Optional[int] = None):
        current = load_job_state(job_id) or {
            "status": "processing",
            "created_at": time.time(),
            "file_path": None,
            "filename": None,
            "error": None,
        }
        current["stage"] = stage_text
        if progress is not None:
            current["progress"] = progress
        save_job_state(job_id, current)

    def progress_hook(d: Dict[str, Any]):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total > 0:
                pct = min(99, max(1, int((downloaded / total) * 100)))
                update_stage(f"Fetching media stream ({pct}%)...", progress=pct)
            else:
                update_stage("Fetching media stream from source...", progress=45)
        elif status == "finished":
            if preset in ("m4a", "mp3"):
                update_stage("Embedding studio metadata & cover art...", progress=92)
            else:
                update_stage("Remuxing video & audio streams via FFmpeg...", progress=92)

    opts = get_base_ydl_opts()
    opts["progress_hooks"] = [progress_hook]
    opts["outtmpl"] = str(job_dir / "%(title).120B.%(ext)s")
    opts["restrictfilenames"] = False

    if preset == "m4a":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        opts["writethumbnail"] = True
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail"},
        ]
    elif preset == "mp3":
        opts["format"] = "bestaudio/best"
        opts["writethumbnail"] = platform in ("youtube", "ytmusic")
        postprocessors = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
        if platform in ("youtube", "ytmusic"):
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
            postprocessors.append({"key": "EmbedThumbnail"})
        opts["postprocessors"] = postprocessors
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        if preset == "high":
            opts["format_sort"] = ["res:1080", "ext:mp4:m4a", "vcodec:h264"]
        elif preset == "medium":
            opts["format_sort"] = ["res:720", "ext:mp4:m4a", "vcodec:h264"]
        elif preset == "low":
            opts["format_sort"] = ["res:480", "ext:mp4:m4a", "vcodec:h264"]

    try:
        update_stage("Connecting to upstream media server...", progress=10)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        media_exts = {".mp4", ".m4a", ".mp3", ".mkv", ".webm", ".opus"}
        files = [
            f for f in job_dir.iterdir()
            if f.is_file() and f.suffix.lower() in media_exts and f.name != "status.json"
        ]
        if not files:
            files = [f for f in job_dir.iterdir() if f.is_file() and f.name != "status.json"]

        if not files:
            raise RuntimeError("Output file was not generated.")

        final_file = max(files, key=lambda p: p.stat().st_size)
        state = load_job_state(job_id) or {"created_at": time.time()}
        state.update({
            "status": "ready",
            "stage": "Completed! Starting download...",
            "progress": 100,
            "file_path": str(final_file),
            "filename": final_file.name,
            "error": None,
        })
        save_job_state(job_id, state)
    except Exception as exc:
        logger.error(f"Download job {job_id} failed: {exc}")
        state = load_job_state(job_id) or {"created_at": time.time()}
        state.update({
            "status": "error",
            "stage": "Error occurred",
            "error": "Failed to process media stream. Please try another quality option.",
        })
        save_job_state(job_id, state)


@app.post("/api/prepare")
async def prepare_download(req: PrepareRequest):
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    initial_state = {
        "status": "processing",
        "stage": "Initializing engine...",
        "progress": 5,
        "created_at": time.time(),
        "file_path": None,
        "filename": None,
        "error": None,
    }
    save_job_state(job_id, initial_state)
    asyncio.create_task(
        asyncio.to_thread(run_download_job, job_id, req.url.strip(), req.preset, req.platform)
    )
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_job_status(job_id: str):
    job = load_job_state(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Session expired or invalid job ID.")
    return {
        "status": job.get("status"),
        "stage": job.get("stage"),
        "progress": job.get("progress", 25),
        "filename": job.get("filename"),
        "error": job.get("error"),
    }


def remove_job_selectively(job_id: str):
    job_dir = TEMP_DIR / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
    JOBS.pop(job_id, None)


@app.get("/api/file/{job_id}")
async def download_ready_file(job_id: str, background_tasks: BackgroundTasks):
    job = load_job_state(job_id)
    if not job or job.get("status") != "ready" or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="File is not ready or has already expired.")

    file_path = Path(job["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File no longer exists on server.")

    background_tasks.add_task(remove_job_selectively, job_id)

    return FileResponse(
        path=file_path,
        filename=job["filename"],
        media_type="application/octet-stream",
    )


# -----------------------------------------------------------------------------
# GitHub Webhook & Auto-Deploy Endpoints
# -----------------------------------------------------------------------------

@app.post("/api/webhook/github")
async def github_webhook_deploy(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
):
    """Receives GitHub Webhook or GitHub Actions notification and triggers Coolify deploy."""
    raw_body = await request.body()

    if WEBHOOK_SECRET and x_hub_signature_256:
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload: Dict[str, Any] = {}
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        pass

    latest_sha = await fetch_latest_github_sha()
    pushed_sha = payload.get("after") or payload.get("commit_sha") or latest_sha

    global LAST_DEPLOY_TRIGGERED_SHA
    if pushed_sha:
        LAST_DEPLOY_TRIGGERED_SHA = pushed_sha

    result = await trigger_coolify_redeploy(reason=f"webhook:{str(pushed_sha)[:7]}")
    return {
        "status": "ok",
        "auto_deploy": result,
        "commit_sha": pushed_sha,
    }


# -----------------------------------------------------------------------------
# Admin Cookie Manager Endpoints (/admin)
# -----------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_cookies_page():
    admin_file = BASE_DIR / "static" / "admin.html"
    if admin_file.exists():
        return HTMLResponse(admin_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="Admin UI not found.")


@app.get("/api/admin/cookies-status")
async def get_cookies_status():
    exists = COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0
    size = COOKIES_FILE.stat().st_size if exists else 0
    updated_at = int(COOKIES_FILE.stat().st_mtime) if exists else None
    return {
        "active": exists,
        "size_bytes": size,
        "updated_at": updated_at,
    }


@app.post("/api/admin/cookies")
async def save_cookies(req: CookieSaveRequest):
    if req.pin.strip() != ADMIN_PIN:
        raise HTTPException(status_code=401, detail="Invalid Admin PIN.")
    content = req.cookies_content.strip()
    if not content:
        if COOKIES_FILE.exists():
            COOKIES_FILE.unlink()
        return {"status": "cleared", "active": False}
    COOKIES_FILE.write_text(content + "\n", encoding="utf-8")
    return {
        "status": "saved",
        "active": True,
        "size_bytes": COOKIES_FILE.stat().st_size,
    }


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "service": "Apun Ka Download",
        "domain": TARGET_FQDN,
        "auto_sync_repo": GITHUB_REPO,
        "boot_commit": BOOT_COMMIT_SHA[:7] if BOOT_COMMIT_SHA else None,
        "cookies_active": COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0,
    }


# Mount static frontend last so API routes take priority
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
