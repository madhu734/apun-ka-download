"""Apun Ka Download - single-container media extractor/downloader.

FastAPI + yt-dlp (in-process) + FFmpeg (remux only for video, no re-encoding).
Serves the REST API and the static frontend on port 8000.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("apun")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/apun-jobs"))
COOKIE_FILE = Path(os.getenv("COOKIE_FILE", str(BASE_DIR / "cookies" / "cookies.txt")))
COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", str(15 * 60)))
CLEANUP_INTERVAL_SECONDS = 60
# "High" tier is capped here so the result stays broadly playable (H.264/AAC MP4).
# Set MAX_VIDEO_RES=2160 to allow 4K where the platform offers it.
MAX_VIDEO_RES = int(os.getenv("MAX_VIDEO_RES", "1080"))

PRIVATE_FB_MESSAGE = (
    "This link belongs to a private Facebook group or restricted post. "
    "Only public Facebook videos can be downloaded."
)
YTM_OVERRIDE_MESSAGE = (
    "YouTube Music links are audio-first, so we switched to Audio Only "
    "and prepared the best audio options for you."
)

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "ytmusic": "YT Music",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}

STANDARD_RES = [144, 240, 360, 480, 540, 576, 720, 1080, 1440, 2160, 4320]
FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_\-.=:]+(\+[A-Za-z0-9_\-.=:]+)?$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    return url


def detect_platform(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()

    def is_host(*domains: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in domains)

    if host == "music.youtube.com":
        return "ytmusic"
    if is_host("youtube.com", "youtube-nocookie.com") or host == "youtu.be":
        return "youtube"
    if is_host("facebook.com", "fb.watch", "fb.gg"):
        return "facebook"
    if is_host("instagram.com"):
        return "instagram"
    if is_host("tiktok.com"):
        return "tiktok"
    return None


def clean_error(exc: BaseException) -> str:
    msg = ANSI_RE.sub("", str(exc))
    msg = re.sub(r"^ERROR:\s*", "", msg.strip())
    msg = re.sub(r"^(\[[^\]]+\]\s*[^:\s]*:\s*)+", "", msg)
    return msg.strip()


def fmt_size(num_bytes: Optional[float]) -> Optional[str]:
    if not num_bytes or num_bytes <= 0:
        return None
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx <= 1:
        return f"{value:.0f} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def fmt_duration(seconds: Optional[float]) -> Optional[str]:
    if not seconds:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def snap_resolution(res: int) -> int:
    for std in STANDARD_RES:
        if abs(res - std) <= max(8, std * 0.03):
            return std
    return res


def base_ydl_opts(workdir: Path) -> dict[str, Any]:
    """Common yt-dlp options. A private copy of the cookie file is used so
    yt-dlp never needs write access to the (possibly read-only) mount."""
    has_cookies = False
    try:
        has_cookies = COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0
    except OSError:
        pass

    # When cookies are present, use web clients that support cookies (web, default).
    # When no cookies are present, use visionos to bypass datacenter IP bot-check.
    yt_clients = ["default", "web", "web_embedded"] if has_cookies else ["visionos"]

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
        "extractor_retries": 2,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
        "geo_bypass": True,
        "noprogress": True,
        "color": "no_color",
        "extractor_args": {
            "youtube": {
                "player_client": yt_clients
            }
        },
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if has_cookies:
        try:
            copy = workdir / "cookies.txt"
            shutil.copyfile(COOKIE_FILE, copy)
            opts["cookiefile"] = str(copy)
        except OSError as exc:  # unreadable mount must never break downloads
            log.warning("Cookie file present but unreadable: %s", exc)
    return opts


# --------------------------------------------------------------------------- #
# Format analysis (resolution bucketing, size estimates)
# --------------------------------------------------------------------------- #
def _size_of(fmt: dict[str, Any], duration: Optional[float]) -> tuple[Optional[float], bool]:
    exact = fmt.get("filesize")
    if exact:
        return float(exact), False
    approx = fmt.get("filesize_approx")
    if approx:
        return float(approx), True
    tbr = fmt.get("tbr") or fmt.get("abr") or fmt.get("vbr")
    if tbr and duration:
        return float(tbr) * 1000 / 8 * float(duration), True
    return None, True


def _is_storyboard(fmt: dict[str, Any]) -> bool:
    return fmt.get("ext") == "mhtml" or str(fmt.get("format_id", "")).startswith("sb") or fmt.get("protocol") == "mhtml"


def _has_video(fmt: dict[str, Any]) -> bool:
    vcodec = fmt.get("vcodec")
    if vcodec == "none":
        return False
    return vcodec is not None or bool(fmt.get("height"))


def _has_audio(fmt: dict[str, Any]) -> bool:
    return fmt.get("acodec") not in (None, "none")


def _video_res(fmt: dict[str, Any]) -> Optional[int]:
    w, h = fmt.get("width"), fmt.get("height")
    if w and h:
        return snap_resolution(int(min(w, h)))  # 1080x1920 (9:16) -> 1080p
    value = h or w
    return snap_resolution(int(value)) if value else None


def _video_rank(fmt: dict[str, Any]) -> tuple:
    vcodec = str(fmt.get("vcodec") or "").lower()
    avc = vcodec.startswith(("avc1", "h264"))
    return (
        1 if avc else 0,
        1 if fmt.get("ext") == "mp4" else 0,
        float(fmt.get("tbr") or fmt.get("vbr") or 0),
    )


def build_video_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    duration = info.get("duration")
    buckets: dict[int, dict[str, Any]] = {}
    audio_only: list[dict[str, Any]] = []

    for fmt in info.get("formats") or []:
        if _is_storyboard(fmt):
            continue
        if "watermark" in str(fmt.get("format_note") or "").lower():
            continue  # TikTok: skip the watermarked variant
        if _has_video(fmt):
            res = _video_res(fmt)
            if not res:
                continue
            best = buckets.get(res)
            if best is None or _video_rank(fmt) > _video_rank(best):
                buckets[res] = fmt
        elif _has_audio(fmt):
            audio_only.append(fmt)

    if not buckets:
        return []

    def audio_key(f: dict[str, Any]) -> float:
        return float(f.get("abr") or f.get("tbr") or 0)

    m4a = [f for f in audio_only if f.get("ext") == "m4a"]
    best_audio = max(m4a, key=audio_key) if m4a else (max(audio_only, key=audio_key) if audio_only else None)
    audio_size, _ = _size_of(best_audio, duration) if best_audio else (0.0, False)

    def selector_for(fmt: dict[str, Any]) -> str:
        fid = str(fmt.get("format_id"))
        if _has_audio(fmt) or not best_audio:
            return fid
        return f"{fid}+{best_audio.get('format_id')}"

    def size_for(fmt: dict[str, Any]) -> tuple[Optional[float], bool]:
        vsize, est = _size_of(fmt, duration)
        if vsize is None:
            return None, True
        if not _has_audio(fmt) and best_audio and audio_size:
            vsize += audio_size
        return vsize, est

    resolutions = sorted(buckets)
    capped = [r for r in resolutions if r <= MAX_VIDEO_RES]
    high = max(capped) if capped else min(resolutions)
    medium_candidates = [r for r in resolutions if 540 <= r <= 780 and r != high]
    medium = min(medium_candidates, key=lambda r: abs(r - 720)) if medium_candidates else None
    low_candidates = [r for r in resolutions if r < 540 and r != high]
    low = max(low_candidates) if low_candidates else None

    plan = [
        ("high", "High / HD" if high >= 720 else "Best Available", high,
         "Best available video + audio, remuxed to MP4 with no re-encoding"),
        ("medium", "Medium", medium,
         "Smart Saver: sharp native resolution at a much smaller size"),
        ("low", "Low", low, "Data Saver: compact file for fast, low-data downloads"),
    ]
    options = []
    for opt_id, label, res, description in plan:
        if res is None:
            continue
        fmt = buckets[res]
        size, estimated = size_for(fmt)
        size_text = fmt_size(size)
        options.append({
            "id": opt_id,
            "kind": "video",
            "label": label,
            "description": description,
            "resolution": f"{res}p",
            "format": "MP4",
            "size_bytes": int(size) if size else None,
            "size_text": (("~" if estimated else "") + size_text) if size_text else None,
            "format_selector": selector_for(fmt),
        })
    return options


def build_audio_options(info: dict[str, Any], platform: str) -> list[dict[str, Any]]:
    duration = info.get("duration")
    mp3_size = float(duration) * 320_000 / 8 if duration else None
    mp3_text = fmt_size(mp3_size)

    if platform in ("youtube", "ytmusic"):
        audio_fmts = [
            f for f in info.get("formats") or []
            if not _is_storyboard(f) and not _has_video(f) and _has_audio(f)
        ]
        m4a = [f for f in audio_fmts if f.get("ext") == "m4a"]
        pool = m4a or audio_fmts
        m4a_size = None
        if pool:
            best = max(pool, key=lambda f: float(f.get("abr") or f.get("tbr") or 0))
            m4a_size, _ = _size_of(best, duration)
        m4a_text = fmt_size(m4a_size)
        return [
            {
                "id": "m4a", "kind": "m4a",
                "label": "Original Studio Audio",
                "description": "Untouched master stream with cover art and track metadata embedded",
                "resolution": "ORIGINAL", "format": "M4A",
                "size_bytes": int(m4a_size) if m4a_size else None,
                "size_text": ("~" + m4a_text) if m4a_text else None,
                "format_selector": None,
            },
            {
                "id": "mp3", "kind": "mp3",
                "label": "Converted MP3",
                "description": "Converted via FFmpeg at 320 kbps with cover art and metadata embedded",
                "resolution": "320 KBPS", "format": "MP3",
                "size_bytes": int(mp3_size) if mp3_size else None,
                "size_text": ("~" + mp3_text) if mp3_text else None,
                "format_selector": None,
            },
        ]
    return [{
        "id": "mp3", "kind": "mp3",
        "label": "Download MP3 Audio",
        "description": "Extracts the audio track directly into a high-quality MP3 file",
        "resolution": "320 KBPS", "format": "MP3",
        "size_bytes": int(mp3_size) if mp3_size else None,
        "size_text": ("~" + mp3_text) if mp3_text else None,
        "format_selector": None,
    }]


# --------------------------------------------------------------------------- #
# Analysis (blocking, runs in a worker thread)
# --------------------------------------------------------------------------- #
PRIVATE_MARKERS = (
    "login required", "log in", "logged in", "private", "unavailable",
    "permission", "not available", "cannot parse data", "restricted",
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def extract_info_blocking(url: str, platform: str) -> dict[str, Any]:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="analyze-", dir=WORK_DIR))
    try:
        opts = base_ydl_opts(tmp)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            msg = clean_error(exc)
            low = msg.lower()
            if platform in ("youtube", "ytmusic") and (
                "bot" in low or "sign in" in low or "login" in low or "drm" in low or "format is not available" in low
            ):
                log.info("Primary extraction hit %s. Retrying with clean visionos client without cookies...", msg[:80])
                opts_fb = base_ydl_opts(tmp)
                opts_fb.pop("cookiefile", None)
                opts_fb["extractor_args"] = {"youtube": {"player_client": ["visionos"]}}
                with yt_dlp.YoutubeDL(opts_fb) as ydl:
                    info = ydl.extract_info(url, download=False)
            else:
                raise
        if not info:
            raise ApiError(422, "No media information could be read from this link.")
        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise ApiError(422, "No downloadable media was found in this post.")
            info = entries[0]
        return info
    except ApiError:
        raise
    except Exception as exc:  # yt_dlp.utils.DownloadError and friends
        msg = clean_error(exc)
        low = msg.lower()
        log.warning("Extraction failed (%s): %s", platform, msg)
        if platform == "facebook":
            path = urlparse(url).path.lower()
            if any(m in low for m in PRIVATE_MARKERS) or "/groups/" in path:
                raise ApiError(403, PRIVATE_FB_MESSAGE)
        if "unsupported url" in low:
            raise ApiError(422, "This link is not supported or does not contain downloadable media.")
        if "no video" in low or "there is no video" in low:
            raise ApiError(422, "This post does not contain a downloadable video.")
        if "sign in" in low or "login" in low or "bot" in low or "cookies" in low:
            raise ApiError(
                422,
                "YouTube requested a bot check or cookies for this media. "
                "Please upload a cookies.txt file on the /admin page or try another link.",
            )
        raise ApiError(422, f"Could not read this link: {msg[:220]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    dir: Path
    created: float = field(default_factory=time.time)
    status: str = "processing"  # processing | ready | error
    stage: str = "Connecting to upstream server..."
    progress: Optional[float] = None
    file: Optional[Path] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task] = None


JOBS: dict[str, Job] = {}

PP_STAGES = {
    "FFmpegThumbnailsConvertor": "Preparing cover art...",
    "FFmpegVideoRemuxer": "Remuxing into MP4 via FFmpeg...",
    "Merger": "Remuxing video & audio streams via FFmpeg...",
    "FFmpegMerger": "Remuxing video & audio streams via FFmpeg...",
    "FFmpegExtractAudio": "Extracting audio via FFmpeg...",
    "FFmpegMetadata": "Embedding studio metadata & cover art...",
    "EmbedThumbnail": "Embedding studio metadata & cover art...",
    "MoveFiles": "Finalizing file...",
}


def build_download_opts(job: Job, kind: str, platform: str, selector: Optional[str]) -> dict[str, Any]:
    opts = base_ydl_opts(job.dir)
    opts["outtmpl"] = str(job.dir / "%(title,id).150B.%(ext)s")
    opts["restrictfilenames"] = False
    opts["windowsfilenames"] = True  # strip characters that break downloads on any OS

    def progress_hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                pct = max(0.0, min(100.0, done * 100.0 / total))
                job.progress = pct
                job.stage = f"Fetching media stream ({pct:.0f}%)..."
            else:
                job.stage = "Fetching media stream..."
        elif d.get("status") == "finished":
            job.progress = 100.0
            job.stage = "Stream received. Starting FFmpeg..."

    def pp_hook(d: dict[str, Any]) -> None:
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            if name == "FFmpegExtractAudio" and kind == "mp3":
                job.stage = "Converting to 320kbps MP3 via FFmpeg..."
            elif name in PP_STAGES:
                job.stage = PP_STAGES[name]

    opts["progress_hooks"] = [progress_hook]
    opts["postprocessor_hooks"] = [pp_hook]

    if kind == "video":
        chosen = selector or "bv*+ba"
        opts["format"] = f"{chosen}/bv*+ba/b"
        opts["merge_output_format"] = "mp4"  # remux only, never re-encode
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    else:
        if kind == "m4a":
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            codec, quality = "m4a", "256"
        else:
            opts["format"] = "bestaudio/best"
            codec, quality = "mp3", "320"
        opts["writethumbnail"] = True
        opts["postprocessors"] = [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ]
    return opts


SQUARE_CROP = "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'"


class SquareCoverPP(FFmpegPostProcessor):
    """Center-crops downloaded cover art to a square (album-art style).
    yt-dlp's own converter skips files that are already JPEG, so this always runs."""

    def run(self, info):
        for thumb in info.get("thumbnails") or []:
            path = thumb.get("filepath")
            if not path or not os.path.isfile(path):
                continue
            tmp = path + ".sq.jpg"
            try:
                self.run_ffmpeg(path, tmp, ["-vf", SQUARE_CROP, "-frames:v", "1", "-q:v", "2"])
                os.replace(tmp, path)
            except Exception as exc:  # cover art is optional; never fail the download
                log.warning("Square-crop of cover art skipped: %s", exc)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return [], info


def new_downloader(job: "Job", kind: str, platform: str, selector: Optional[str]) -> yt_dlp.YoutubeDL:
    ydl = yt_dlp.YoutubeDL(build_download_opts(job, kind, platform, selector))
    if kind in ("m4a", "mp3"):
        ydl.add_post_processor(SquareCoverPP(ydl), when="before_dl")  # runs after the JPG conversion
    return ydl


MEDIA_EXTS = {".mp4", ".m4a", ".mp3"}


def locate_output(job: Job, info: Optional[dict[str, Any]]) -> Optional[Path]:
    if info:
        for dl in info.get("requested_downloads") or []:
            candidate = Path(dl.get("filepath") or "")
            if candidate.is_file() and candidate.suffix.lower() in MEDIA_EXTS:
                return candidate
    files = [p for p in job.dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def run_download_blocking(job: Job, url: str, kind: str, platform: str, selector: Optional[str]) -> None:
    try:
        try:
            with new_downloader(job, kind, platform, selector) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:
            msg = clean_error(exc)
            low = msg.lower()
            if platform in ("youtube", "ytmusic") and (
                "bot" in low or "sign in" in low or "login" in low or "drm" in low or "format is not available" in low
            ):
                log.info("Download hit %s. Retrying download with visionos client without cookies...", msg[:80])
                opts_fb = build_download_opts(job, kind, platform, selector)
                opts_fb.pop("cookiefile", None)
                opts_fb["extractor_args"] = {"youtube": {"player_client": ["visionos"]}}
                with yt_dlp.YoutubeDL(opts_fb) as ydl:
                    if kind in ("m4a", "mp3"):
                        ydl.add_post_processor(SquareCoverPP(ydl), when="before_dl")
                    info = ydl.extract_info(url, download=True)
            else:
                raise
        if info and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else info
        out = locate_output(job, info)
        if not out:
            raise RuntimeError("The file could not be produced.")
        job.file, job.filename = out, out.name
        job.progress = 100.0
        job.stage = "Ready. Starting your download..."
        job.status = "ready"
    except Exception as exc:
        msg = clean_error(exc)
        log.warning("Job %s failed: %s", job.id, msg)
        low = msg.lower()
        if platform == "facebook" and any(m in low for m in PRIVATE_MARKERS):
            msg = PRIVATE_FB_MESSAGE
        job.error = msg[:300] or "The download failed."
        job.stage = "Failed."
        job.status = "error"


async def run_job(job: Job, url: str, kind: str, platform: str, selector: Optional[str]) -> None:
    await asyncio.to_thread(run_download_blocking, job, url, kind, platform, selector)


def drop_job(job_id: str) -> None:
    job = JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job.dir, ignore_errors=True)


def sweep_stale() -> None:
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if now - job.created > JOB_TTL_SECONDS:
            drop_job(job_id)
    if WORK_DIR.is_dir():
        for child in WORK_DIR.iterdir():
            try:
                if now - child.stat().st_mtime > JOB_TTL_SECONDS and child.name not in JOBS:
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
            except OSError:
                pass


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(sweep_stale)
        except Exception:  # never let the janitor die
            log.exception("Cleanup sweep failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        Path(COOKIE_FILE).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    janitor = asyncio.create_task(cleanup_loop())
    log.info("Apun Ka Download ready. yt-dlp %s, ffmpeg=%s", yt_dlp.version.__version__, bool(shutil.which("ffmpeg")))
    try:
        yield
    finally:
        janitor.cancel()
        shutil.rmtree(WORK_DIR, ignore_errors=True)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
app = FastAPI(title="Apun Ka Download", lifespan=lifespan, docs_url=None, redoc_url=None)


class AnalyzeRequest(BaseModel):
    url: str
    mode: Literal["video", "audio"] = "video"


class PrepareRequest(BaseModel):
    url: str
    kind: Literal["video", "m4a", "mp3"]
    format_selector: Optional[str] = None


def validated_url(raw: str) -> tuple[str, str]:
    url = normalize_url(raw)
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(
            400,
            "Unsupported link. Paste a YouTube, YT Music, Facebook, Instagram or TikTok URL.",
        )
    return url, platform


@app.get("/api/health")
async def health() -> dict[str, Any]:
    cookies = False
    try:
        cookies = COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0
    except OSError:
        pass
    return {
        "status": "ok",
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "yt_dlp": yt_dlp.version.__version__,
        "cookies": cookies,
        "active_jobs": sum(1 for j in JOBS.values() if j.status == "processing"),
    }


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    url, platform = validated_url(req.url)
    mode, overridden, override_message = req.mode, False, None

    if platform == "ytmusic" and mode == "video":
        mode, overridden, override_message = "audio", True, YTM_OVERRIDE_MESSAGE

    try:
        info = await asyncio.wait_for(asyncio.to_thread(extract_info_blocking, url, platform), timeout=90)
    except asyncio.TimeoutError:
        raise HTTPException(504, "The platform took too long to respond. Please try again.")
    except ApiError as err:
        raise HTTPException(err.status, err.message)

    options: list[dict[str, Any]] = []
    if mode == "video":
        options = build_video_options(info)
        if not options:  # audio-only item: fall back instead of failing
            mode, overridden = "audio", True
            override_message = "No video stream exists for this link, so we switched to Audio Only."
    if mode == "audio":
        options = build_audio_options(info, platform)

    return {
        "platform": platform,
        "platform_label": PLATFORM_LABELS[platform],
        "mode": mode,
        "mode_overridden": overridden,
        "override_message": override_message,
        "url": url,
        "title": info.get("track") or info.get("title") or "Untitled",
        "uploader": info.get("artist") or info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "duration_text": fmt_duration(info.get("duration")),
        "thumbnail": info.get("thumbnail"),
        "options": options,
    }


@app.post("/api/prepare")
async def prepare(req: PrepareRequest) -> dict[str, str]:
    url, platform = validated_url(req.url)
    if req.kind == "m4a" and platform not in ("youtube", "ytmusic"):
        raise HTTPException(400, "Original M4A audio is only offered for YouTube and YT Music links.")
    selector = None
    if req.kind == "video":
        if req.format_selector:
            if not FORMAT_ID_RE.match(req.format_selector):
                raise HTTPException(400, "Invalid format selection.")
            selector = req.format_selector
    job_id = uuid.uuid4().hex
    workdir = WORK_DIR / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    job = Job(id=job_id, dir=workdir)
    JOBS[job_id] = job
    job.task = asyncio.create_task(run_job(job, url, req.kind, platform, selector))
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def status(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "This download expired. Please start again.")
    return {
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "error": job.error,
        "filename": job.filename if job.status == "ready" else None,
    }


@app.get("/api/file/{job_id}")
async def get_file(job_id: str, background: BackgroundTasks) -> FileResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "This download expired. Please start again.")
    if job.status != "ready" or not job.file or not job.file.is_file():
        raise HTTPException(409, "The file is not ready yet.")
    media_types = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg"}
    background.add_task(drop_job, job_id)  # delete the temp folder once delivery completes
    return FileResponse(
        job.file,
        media_type=media_types.get(job.file.suffix.lower(), "application/octet-stream"),
        filename=job.filename,
    )



# --------------------------------------------------------------------------- #
# Standalone Admin & Cookies Management (Password-free as per user preference)
# --------------------------------------------------------------------------- #
@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    admin_file = STATIC_DIR / "admin.html"
    if admin_file.is_file():
        return HTMLResponse(content=admin_file.read_text(encoding="utf-8"))
    raise HTTPException(404, "Admin page not found.")


@app.get("/api/admin/cookies-status")
async def get_cookies_status() -> dict[str, Any]:
    active = COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0
    size = COOKIE_FILE.stat().st_size if active else 0
    updated_at = (
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(COOKIE_FILE.stat().st_mtime))
        if active
        else None
    )
    lines_count = 0
    platforms = []
    if active:
        try:
            content = COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
            lines_count = len([line for line in content.splitlines() if line.strip() and not line.strip().startswith("#")])
            content_lower = content.lower()
            for key, name in [
                ("youtube", "YouTube"),
                ("facebook", "Facebook"),
                ("instagram", "Instagram"),
                ("tiktok", "TikTok"),
            ]:
                if key in content_lower:
                    platforms.append(name)
        except Exception:
            pass

    return {
        "active": active,
        "size_bytes": size,
        "updated_at": updated_at,
        "lines_count": lines_count,
        "platforms": platforms,
    }


@app.get("/api/admin/cookies-content")
async def get_cookies_content() -> dict[str, str]:
    if COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0:
        try:
            return {"content": COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")}
        except Exception as exc:
            raise HTTPException(500, f"Cannot read cookie file: {exc}")
    return {"content": ""}


class CookieSaveRequest(BaseModel):
    cookies_content: str


@app.post("/api/admin/cookies")
async def save_cookies(req: CookieSaveRequest) -> dict[str, Any]:
    content = req.cookies_content.strip()
    if not content:
        raise HTTPException(400, "Cookie content cannot be empty.")
    try:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(content, encoding="utf-8")
        return {
            "status": "success",
            "message": "Cookies saved successfully.",
            "size_bytes": COOKIE_FILE.stat().st_size,
        }
    except Exception as exc:
        raise HTTPException(500, f"Failed to save cookies: {exc}")


@app.delete("/api/admin/cookies")
async def delete_cookies() -> dict[str, Any]:
    try:
        if COOKIE_FILE.is_file():
            COOKIE_FILE.unlink()
        return {"status": "success", "message": "Cookies deleted successfully."}
    except Exception as exc:
        raise HTTPException(500, f"Failed to delete cookies: {exc}")

# Static frontend last, so /api/* always wins.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
