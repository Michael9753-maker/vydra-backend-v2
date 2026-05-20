from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

import yt_dlp
from yt_dlp.utils import DownloadError

logger = logging.getLogger(__name__)

# 📁 Download directory setup
DEFAULT_DOWNLOAD_DIR = Path(__file__).resolve().parents[2] / "downloads"
DOWNLOAD_PATH = Path(os.getenv("DOWNLOADS_DIR", str(DEFAULT_DOWNLOAD_DIR))).expanduser().resolve()
DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)

DOWNLOAD_URL_PREFIX = "/api/download/file/"

POSSIBLE_EXTENSIONS = (
    "mp4", "mkv", "webm", "mov", "m4v",
    "flv", "avi", "mp3", "m4a", "aac",
    "opus", "ts",
)

# 🍪 Cookies (optional)
DEFAULT_COOKIE_FILE = Path(__file__).resolve().parents[2] / "tiktok_cookies.txt"
COOKIE_FILE = Path(os.getenv("YTDLP_COOKIEFILE", str(DEFAULT_COOKIE_FILE))).expanduser().resolve()
COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
COOKIES_CONTENT = os.getenv("YTDLP_COOKIES_CONTENT", "").strip()
RUNTIME_COOKIE_FILE = Path(
    os.getenv("YTDLP_RUNTIME_COOKIEFILE", str(Path(__file__).resolve().parents[2] / "tiktok_cookies.runtime.txt"))
).expanduser().resolve()

if not COOKIE_FILE.exists():
    logger.warning("⚠️ Cookie file not found: %s", COOKIE_FILE)


# 🔤 Clean filename
def clean_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|#]', "", str(title or ""))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:100]


# 🔍 Input resolvers
def _pick_url(url: Optional[str] = None, **kwargs) -> str:
    candidate = url or kwargs.get("video_url") or kwargs.get("source_url") or kwargs.get("link")
    if not candidate:
        raise ValueError("url is required")
    return str(candidate).strip()


def _pick_user_id(user_id: Optional[str] = None, **kwargs) -> str:
    return str(user_id or kwargs.get("user_id") or "").strip()


def _pick_job_id(job_id: Optional[str] = None, **kwargs) -> str:
    return str(job_id or kwargs.get("job_id") or "").strip()


# 📂 File helpers
def _iter_existing(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        try:
            if p.exists() and p.is_file():
                yield p
        except Exception:
            continue


def _prepare_cookie_file() -> Optional[str]:
    """
    Returns a valid cookie file path if available.
    Priority:
      1) YTDLP_COOKIES_FROM_BROWSER
      2) Existing YTDLP_COOKIEFILE path
      3) YTDLP_COOKIES_CONTENT written to a runtime file
    """
    if COOKIES_FROM_BROWSER:
        return None

    if COOKIE_FILE.exists() and COOKIE_FILE.is_file():
        return str(COOKIE_FILE.resolve())

    if COOKIES_CONTENT:
        try:
            RUNTIME_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            RUNTIME_COOKIE_FILE.write_text(COOKIES_CONTENT, encoding="utf-8")
            logger.info("Created runtime cookie file: %s", str(RUNTIME_COOKIE_FILE))
            return str(RUNTIME_COOKIE_FILE.resolve())
        except Exception as exc:
            logger.error("Failed to write runtime cookie file: %s", exc)

    return None


# 🔧 yt-dlp config
def _build_ydl_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "outtmpl": str(DOWNLOAD_PATH / "%(extractor)s_%(id)s.%(ext)s"),
        "format": "mp4/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,

        # Keep logs readable while still letting errors surface in Railway logs
        "quiet": True,
        "no_warnings": True,

        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "force_ipv4": True,
        "geo_bypass": True,
        "continuedl": True,
        "overwrites": True,
        "ignoreerrors": False,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/16.0 Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        },
    }

    if COOKIES_FROM_BROWSER:
        logger.info("Using browser cookies: %s", COOKIES_FROM_BROWSER)
        opts["cookiesfrombrowser"] = COOKIES_FROM_BROWSER
    else:
        cookie_path = _prepare_cookie_file()
        if cookie_path:
            logger.info("Using cookie file: %s", cookie_path)
            opts["cookiefile"] = cookie_path
        else:
            logger.warning("⚠️ No cookies found — YouTube/TikTok may block requests")

    return opts


# 📂 Resolve downloaded file
def _resolve_downloaded_file(info: Dict[str, Any], prepared_filename: str) -> Optional[Path]:
    candidates: list[Path] = []

    for key in ("filepath", "_filename"):
        value = info.get(key)
        if value:
            candidates.append(Path(str(value)))

    for item in info.get("requested_downloads") or []:
        if isinstance(item, dict):
            for key in ("filepath", "filename"):
                value = item.get(key)
                if value:
                    candidates.append(Path(str(value)))

    if prepared_filename:
        prepared_path = Path(prepared_filename)
        candidates.append(prepared_path)

        stem = prepared_path.with_suffix("")
        for ext in POSSIBLE_EXTENSIONS:
            candidates.append(stem.with_suffix(f".{ext}"))

    existing = list(_iter_existing(candidates))
    if existing:
        for candidate in existing:
            if candidate.suffix.lower() == ".mp4":
                return candidate
        return existing[0]

    return None


# 🔗 Build download URL
def _build_download_url(file_path: Path) -> str:
    base_url = os.getenv("BASE_URL", "").rstrip("/")
    return f"{base_url}{DOWNLOAD_URL_PREFIX}{quote(file_path.name)}"


# 🚀 MAIN FUNCTION
def process_download(
    url: Optional[str] = None,
    user_id: Optional[str] = None,
    job_id: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    video_url = _pick_url(url, **kwargs)
    resolved_user_id = _pick_user_id(user_id, **kwargs)
    resolved_job_id = _pick_job_id(job_id, **kwargs)

    logger.info("🚀 Download start: %s", video_url)

    try:
        ydl_opts = _build_ydl_opts()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)

            prepared_filename = ydl.prepare_filename(info)
            resolved_path = _resolve_downloaded_file(info, prepared_filename)

            if resolved_path is None:
                raise Exception("Download finished but file was not found on disk")

            absolute_file_path = resolved_path.resolve()

            return {
                "success": True,
                "status": "SUCCESS",
                "message": "Download completed",
                "download_url": _build_download_url(absolute_file_path),
                "file_name": absolute_file_path.name,
                "file_path": str(absolute_file_path),
                "job_id": resolved_job_id,
                "user_id": resolved_user_id,
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
            }

    except DownloadError as e:
        error_msg = str(e)
        logger.exception("yt-dlp error")

        return {
            "success": False,
            "status": "FAILURE",
            "message": error_msg,
            "download_url": None,
            "file_name": "",
            "file_path": "",
            "job_id": resolved_job_id,
            "user_id": resolved_user_id,
            "url": video_url,
        }

    except Exception as e:
        error_msg = str(e)
        logger.exception("Unexpected error")

        return {
            "success": False,
            "status": "ERROR",
            "message": error_msg,
            "download_url": None,
            "file_name": "",
            "file_path": "",
            "job_id": resolved_job_id,
            "user_id": resolved_user_id,
            "url": video_url,
        }