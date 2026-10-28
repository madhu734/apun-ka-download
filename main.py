"""Apun Ka Download - single-container media extractor/downloader.

FastAPI + yt-dlp (in-process) + FFmpeg (remux only for video, no re-encoding).
Serves the REST API and the static frontend on port 8000.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
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
from typing import Any, Dict, Literal, Optional
from urllib.parse import urlparse

import httpx
import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
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
MAX_VIDEO_RES = int(os.getenv("MAX_VIDEO_RES", "1080"))

# Coolify & GitHub Auto-Deploy / Cookie Persistence Config
GITHUB_REPO = os.environ.get("GITHUB_REPO", "madhu734/apun-ka-download").strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
COOLIFY_BASE_URL = os.environ.get("COOLIFY_BASE_URL", "http://140.238.160.99:8000/api/v1").strip()
COOLIFY_API_TOKEN = os.environ.get("COOLIFY_API_TOKEN", "").strip()
COOLIFY_APP_UUID = os.environ.get("COOLIFY_APP_UUID", "iruropwov7dpshz81pxprsxf").strip()
TARGET_FQDN = os.environ.get("COOLIFY_DOMAIN", os.environ.get("TARGET_FQDN", "https://download.tmadhu.me")).strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

BOOT_COMMIT_SHA: Optional[str] = None
LAST_DEPLOY_TRIGGERED_SHA: Optional[str] = None

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
# Cookie Normalization & Coolify Persistence Helpers
# --------------------------------------------------------------------------- #
def normalize_cookies_content(raw: str) -> str:
    """Normalizes either JSON cookie exports (EditThisCookie/Cookie-Editor) or
    Netscape cookie text into a strict Netscape HTTP Cookie File for yt-dlp."""
    text = (raw or "").strip()
    if not text:
        return ""

    # 1. Check if the user pasted JSON array of cookies
    if text.startswith("[") and text.endswith("]"):
        try:
            items = json.loads(text)
            if isinstance(items, list):
                lines = ["# Netscape HTTP Cookie File", "# Converted from JSON cookie export", ""]
                for c in items:
                    if not isinstance(c, dict) or not c.get("name"):
                        continue
                    domain = str(c.get("domain") or "").strip()
                    if not domain:
                        continue
                    include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                    path = str(c.get("path") or "/")
                    secure = "TRUE" if c.get("secure") else "FALSE"
                    exp = int(c.get("expirationDate") or c.get("expiry") or (int(time.time()) + 86400 * 365))
                    name = str(c.get("name")).strip()
                    val = str(c.get("value") or "").strip()
                    lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{exp}\t{name}\t{val}")
                return "\n".join(lines) + "\n"
        except Exception as exc:
            log.warning("Failed to parse JSON cookies, falling back to Netscape parser: %s", exc)

    # 2. Normalize Netscape format lines
    raw_lines = [ln.rstrip("\r") for ln in text.splitlines()]
    out_lines = ["# Netscape HTTP Cookie File"]
    for ln in raw_lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith("# Netscape HTTP Cookie File") or stripped.startswith("# HTTP Cookie File"):
            continue
        if stripped.startswith("#HttpOnly_"):
            # Keep #HttpOnly_ prefix supported by curl/yt-dlp, ensure tab-separated fields
            parts = stripped.split("\t")
            if len(parts) < 7:
                parts = re.split(r"\s+", stripped, maxsplit=6)
            if len(parts) == 7:
                out_lines.append("\t".join(parts))
            continue
        if stripped.startswith("#"):
            out_lines.append(stripped)
            continue
        parts = stripped.split("\t")
        if len(parts) < 7:
            parts = re.split(r"\s+", stripped, maxsplit=6)
        if len(parts) == 7:
            domain, flag, path, secure, expiry, name, value = parts
            flag = "TRUE" if domain.startswith(".") else flag.upper()
            secure = "TRUE" if secure.upper() == "TRUE" else "FALSE"
            if not expiry.isdigit():
                expiry = str(int(time.time()) + 86400 * 365)
            out_lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
        else:
            out_lines.append(ln)

    return "\n".join(out_lines) + "\n"


async def sync_cookies_to_coolify_env(content: str) -> None:
    """Persists cookies.txt in Coolify's SAVED_COOKIES_B64 env var so container
    rebuilds/redeployments never lose uploaded cookies."""
    if not COOLIFY_API_TOKEN or not COOLIFY_APP_UUID:
        return
    b64_val = base64.b64encode(content.encode("utf-8")).decode("ascii") if content else ""
    base = COOLIFY_BASE_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {COOLIFY_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.patch(
                f"{base}/applications/{COOLIFY_APP_UUID}/envs/bulk",
                headers=headers,
                json={"data": [{"key": "SAVED_COOKIES_B64", "value": b64_val, "is_runtime": True, "is_buildtime": False}]},
            )
            log.info("Synced cookies.txt to Coolify persistent env (SAVED_COOKIES_B64, %d bytes)", len(content))
    except Exception as exc:
        log.warning("Could not sync cookies to Coolify env: %s", exc)


async def restore_cookies_from_coolify_env() -> None:
    """Restores cookies.txt on container startup if wiped by a container rebuild."""
    try:
        if COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0:
            return
    except OSError:
        pass

    b64_val = os.environ.get("SAVED_COOKIES_B64", "").strip()
    if not b64_val and COOLIFY_API_TOKEN and COOLIFY_APP_UUID:
        base = COOLIFY_BASE_URL.rstrip("/")
        headers = {"Authorization": f"Bearer {COOLIFY_API_TOKEN}", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.get(f"{base}/applications/{COOLIFY_APP_UUID}/envs", headers=headers)
                if res.status_code == 200:
                    for item in res.json():
                        if item.get("key") == "SAVED_COOKIES_B64" and item.get("real_value"):
                            b64_val = item["real_value"].strip()
                            break
        except Exception as exc:
            log.debug("Could not fetch SAVED_COOKIES_B64 from Coolify API: %s", exc)

    if b64_val:
        try:
            decoded = base64.b64decode(b64_val).decode("utf-8", errors="ignore")
            normalized = normalize_cookies_content(decoded)
            if normalized:
                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                COOKIE_FILE.write_text(normalized, encoding="utf-8")
                log.info("Restored cookies.txt from Coolify persistent storage (%d bytes)", len(normalized))
        except Exception as exc:
            log.warning("Failed to restore cookies from SAVED_COOKIES_B64: %s", exc)


# --------------------------------------------------------------------------- #
# Auto-Deploy & Domain Helpers
# --------------------------------------------------------------------------- #
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
        log.debug("GitHub SHA check failed: %s", exc)
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
        log.info("Coolify auto-deploy triggered (%s): HTTP %s", reason, res.status_code)
        return {
            "triggered": res.status_code < 400,
            "status_code": res.status_code,
            "reason": reason,
        }


async def github_auto_sync_watcher() -> None:
    global BOOT_COMMIT_SHA, LAST_DEPLOY_TRIGGERED_SHA
    await asyncio.sleep(2)
    await restore_cookies_from_coolify_env()
    BOOT_COMMIT_SHA = await fetch_latest_github_sha()
    log.info("Initialized GitHub Auto-Sync Watcher for %s@%s (SHA: %s)", GITHUB_REPO, GITHUB_BRANCH, BOOT_COMMIT_SHA)

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
                log.info("New commit detected on %s: %s (was %s)", GITHUB_REPO, latest_sha[:7], BOOT_COMMIT_SHA[:7])
                LAST_DEPLOY_TRIGGERED_SHA = latest_sha
                await trigger_coolify_redeploy(reason=f"auto_watcher:{latest_sha[:7]}")
        except Exception as exc:
            log.debug("Auto-sync watcher error: %s", exc)


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


def has_valid_cookies() -> bool:
    try:
        return COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0
    except OSError:
        return False


def base_ydl_opts(workdir: Path, use_cookies: Optional[bool] = None, yt_clients: Optional[list[str]] = None) -> dict[str, Any]:
    """Common yt-dlp options. A private copy of the cookie file is used so
    yt-dlp never needs write access to the shared cookie file."""
    cookie_present = has_valid_cookies() if use_cookies is None else (use_cookies and has_valid_cookies())

    if yt_clients is None:
        yt_clients = ["web", "mweb", "tv_embedded"] if cookie_present else ["ios", "visionos", "tv"]

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
        "nocheckcertificate": True,
        "noprogress": True,
        "color": "no_color",
        "extractor_args": {
            "youtube": {
                "player_client": yt_clients,
            }
        },
        "js_runtimes": {"node": {}},
    }
    if cookie_present:
        try:
            raw_cookie = COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
            normalized = normalize_cookies_content(raw_cookie)
            copy = workdir / "cookies.txt"
            copy.write_text(normalized, encoding="utf-8")
            opts["cookiefile"] = str(copy)
        except OSError as exc:
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
            continue
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
        info = None
        last_exc: Optional[Exception] = None

        if platform in ("youtube", "ytmusic"):
            # Try cascade of strategies so expired cookies or datacenter bot-checks never block public videos
            strategies: list[tuple[bool, list[str]]] = []
            if has_valid_cookies():
                strategies.append((True, ["web", "mweb", "tv_embedded"]))
                strategies.append((True, ["ios", "visionos", "tv"]))
            strategies.append((False, ["ios", "visionos", "tv"]))
            strategies.append((False, ["android_vr", "mweb", "web_embedded"]))

            for use_c, clients in strategies:
                try:
                    opts = base_ydl_opts(tmp, use_cookies=use_c, yt_clients=clients)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        candidate = ydl.extract_info(url, download=False)
                    if candidate and (candidate.get("formats") or candidate.get("entries") or candidate.get("url")):
                        info = candidate
                        break
                except Exception as exc:
                    last_exc = exc
                    log.info("YouTube strategy (cookies=%s, clients=%s) failed: %s", use_c, clients, clean_error(exc)[:100])
            if not info and last_exc:
                raise last_exc
        else:
            opts = base_ydl_opts(tmp)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

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
    except Exception as exc:
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
                "YouTube requested a bot check or fresh cookies for this media. "
                "Please update your cookies.txt file on the /admin page or try another link.",
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


def build_download_opts(
    job: Job,
    kind: str,
    platform: str,
    selector: Optional[str],
    use_cookies: Optional[bool] = None,
    yt_clients: Optional[list[str]] = None,
) -> dict[str, Any]:
    opts = base_ydl_opts(job.dir, use_cookies=use_cookies, yt_clients=yt_clients)
    opts["outtmpl"] = str(job.dir / "%(title,id).150B.%(ext)s")
    opts["restrictfilenames"] = False
    opts["windowsfilenames"] = True

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
        opts["format"] = f"{chosen}/bv*[height<={MAX_VIDEO_RES}]+ba/b[height<={MAX_VIDEO_RES}]/bv*+ba/b"
        opts["merge_output_format"] = "mp4"
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
    """Center-crops downloaded cover art to a square (album-art style)."""

    def run(self, info):
        for thumb in info.get("thumbnails") or []:
            path = thumb.get("filepath")
            if not path or not os.path.isfile(path):
                continue
            tmp = path + ".sq.jpg"
            try:
                self.run_ffmpeg(path, tmp, ["-vf", SQUARE_CROP, "-frames:v", "1", "-q:v", "2"])
                os.replace(tmp, path)
            except Exception as exc:
                log.warning("Square-crop of cover art skipped: %s", exc)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return [], info


def new_downloader(
    job: Job,
    kind: str,
    platform: str,
    selector: Optional[str],
    use_cookies: Optional[bool] = None,
    yt_clients: Optional[list[str]] = None,
) -> yt_dlp.YoutubeDL:
    ydl = yt_dlp.YoutubeDL(
        build_download_opts(job, kind, platform, selector, use_cookies=use_cookies, yt_clients=yt_clients)
    )
    if kind in ("m4a", "mp3"):
        ydl.add_post_processor(SquareCoverPP(ydl), when="before_dl")
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
        info = None
        last_exc: Optional[Exception] = None

        if platform in ("youtube", "ytmusic"):
            strategies: list[tuple[bool, list[str], Optional[str]]] = []
            if has_valid_cookies():
                strategies.append((True, ["web", "mweb", "tv_embedded"], selector))
                strategies.append((True, ["ios", "visionos", "tv"], None))
            strategies.append((False, ["ios", "visionos", "tv"], selector))
            strategies.append((False, ["android_vr", "mweb", "web_embedded"], None))

            for use_c, clients, sel in strategies:
                try:
                    with new_downloader(job, kind, platform, sel, use_cookies=use_c, yt_clients=clients) as ydl:
                        info = ydl.extract_info(url, download=True)
                    out_check = locate_output(job, info)
                    if out_check:
                        break
                except Exception as exc:
                    last_exc = exc
                    log.info("Download strategy (cookies=%s, clients=%s) failed: %s", use_c, clients, clean_error(exc)[:100])
            if not locate_output(job, info) and last_exc:
                raise last_exc
        else:
            with new_downloader(job, kind, platform, selector) as ydl:
                info = ydl.extract_info(url, download=True)

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
        except Exception:
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
    watcher = asyncio.create_task(github_auto_sync_watcher())
    log.info("Apun Ka Download ready. yt-dlp %s, ffmpeg=%s", yt_dlp.version.__version__, bool(shutil.which("ffmpeg")))
    try:
        yield
    finally:
        janitor.cancel()
        watcher.cancel()
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
    cookies = has_valid_cookies()
    return {
        "status": "ok",
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "node": bool(shutil.which("node")),
        "yt_dlp": yt_dlp.version.__version__,
        "cookies": cookies,
        "boot_commit": BOOT_COMMIT_SHA[:7] if BOOT_COMMIT_SHA else None,
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
        if not options:
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
    background.add_task(drop_job, job_id)
    return FileResponse(
        job.file,
        media_type=media_types.get(job.file.suffix.lower(), "application/octet-stream"),
        filename=job.filename,
    )


# --------------------------------------------------------------------------- #
# GitHub Webhook & Auto-Deploy Endpoint
# --------------------------------------------------------------------------- #
@app.post("/api/webhook/github")
async def github_webhook_deploy(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    raw_body = await request.body()
    if WEBHOOK_SECRET and x_hub_signature_256:
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload: dict[str, Any] = {}
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


# --------------------------------------------------------------------------- #
# Standalone Admin & Cookies Management (Password-free as per user preference)
# --------------------------------------------------------------------------- #
def _inspect_cookie_stats() -> dict[str, Any]:
    active = has_valid_cookies()
    size = COOKIE_FILE.stat().st_size if active else 0
    updated_at = int(COOKIE_FILE.stat().st_mtime) if active else None
    lines_count = 0
    platforms: list[str] = []
    if active:
        try:
            content = COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
            lines_count = len(
                [line for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
            )
            content_lower = content.lower()
            for key, name in [
                ("youtube", "YouTube"),
                ("googlevideo", "YouTube"),
                ("facebook", "Facebook"),
                ("fb.com", "Facebook"),
                ("instagram", "Instagram"),
                ("tiktok", "TikTok"),
            ]:
                if key in content_lower and name not in platforms:
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


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    admin_file = STATIC_DIR / "admin.html"
    if admin_file.is_file():
        return HTMLResponse(content=admin_file.read_text(encoding="utf-8"))
    raise HTTPException(404, "Admin page not found.")


@app.get("/api/admin/cookies-status")
async def get_cookies_status() -> dict[str, Any]:
    return _inspect_cookie_stats()


@app.get("/api/admin/cookies-content")
async def get_cookies_content() -> dict[str, str]:
    if has_valid_cookies():
        try:
            return {"content": COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")}
        except Exception as exc:
            raise HTTPException(500, f"Cannot read cookie file: {exc}")
    return {"content": ""}


class CookieSaveRequest(BaseModel):
    cookies_content: str = ""
    pin: Optional[str] = None


@app.post("/api/admin/cookies")
async def save_cookies(req: CookieSaveRequest) -> dict[str, Any]:
    raw_content = (req.cookies_content or "").strip()
    if not raw_content:
        if COOKIE_FILE.is_file():
            COOKIE_FILE.unlink(missing_ok=True)
        await sync_cookies_to_coolify_env("")
        stats = _inspect_cookie_stats()
        return {"status": "cleared", "message": "Cookies cleared.", **stats}

    try:
        normalized = normalize_cookies_content(raw_content)
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(normalized, encoding="utf-8")
        await sync_cookies_to_coolify_env(normalized)
        stats = _inspect_cookie_stats()
        return {
            "status": "success",
            "message": "Cookies saved and activated successfully.",
            **stats,
        }
    except Exception as exc:
        raise HTTPException(500, f"Failed to save cookies: {exc}")


@app.delete("/api/admin/cookies")
async def delete_cookies() -> dict[str, Any]:
    try:
        if COOKIE_FILE.is_file():
            COOKIE_FILE.unlink(missing_ok=True)
        await sync_cookies_to_coolify_env("")
        stats = _inspect_cookie_stats()
        return {"status": "success", "message": "Cookies deleted successfully.", **stats}
    except Exception as exc:
        raise HTTPException(500, f"Failed to delete cookies: {exc}")


# Static frontend last, so /api/* always wins.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
