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
DEFAULT_COOKIE_FILE = Path(__file__).resolve().parents[2] / "cookies.txt"
COOKIE_FILE_ENV = os.getenv("YTDLP_COOKIEFILE", str(DEFAULT_COOKIE_FILE)).strip()
COOKIE_FILE = Path(COOKIE_FILE_ENV).expanduser()
COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
COOKIES_CONTENT = os.getenv("YTDLP_COOKIES_CONTENT", "").strip()
RUNTIME_COOKIE_FILE = Path(os.getenv("YTDLP_RUNTIME_COOKIEFILE", "/tmp/ytdlp_cookies.txt")).expanduser()

# 🌐 Optional proxy support (leave empty unless you intentionally set one)
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()


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
    candidate = user_id or kwargs.get("user_id") or kwargs.get("email") or kwargs.get("user_email")
    return str(candidate).strip() if candidate is not None else ""


def _pick_job_id(job_id: Optional[str] = None, **kwargs) -> str:
    candidate = job_id or kwargs.get("job_id") or kwargs.get("task_id")
    return str(candidate).strip() if candidate is not None else ""


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


def _build_ydl_opts() -> Dict[str, Any]:
    """
    Production-grade yt-dlp config for stability and higher success rate.
    """
    opts = {
        # 📁 Output
        "outtmpl": str(DOWNLOAD_PATH / "%(extractor)s_%(id)s.%(ext)s"),

        # ⚡ FAST FORMAT
        "format": "mp4/best[ext=mp4]/best",

        "merge_output_format": "mp4",
        "noplaylist": True,

        # 🔇 Silent
        "quiet": True,
        "no_warnings": True,

        # 🌐 Network tuning
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,

        # 🔥 Prefer IPv4 in hosted environments
        "force_ipv4": True,

        # 🌍 Improve success rate for geo-sensitive extraction
        "geo_bypass": True,

        # ⚙️ Download behavior
        "continuedl": True,
        "overwrites": True,
        "ignoreerrors": False,

        # 📂 File safety
        "windowsfilenames": True,
        "restrictfilenames": False,

        # 🌐 Headers
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/16.0 Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        },

        # ⚡ Better YouTube extraction profile
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "web"],
                "player_skip": ["webpage", "configs"],
            }
        },
    }

    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
        logger.info("Using yt-dlp proxy from environment")

    # 🍪 COOKIE SUPPORT
    if COOKIES_FROM_BROWSER:
        logger.info("Using browser cookies: %s", COOKIES_FROM_BROWSER)
        opts["cookiesfrombrowser"] = COOKIES_FROM_BROWSER
    else:
        cookie_path = _prepare_cookie_file()
        if cookie_path:
            logger.info("Using cookie file: %s", cookie_path)
            opts["cookiefile"] = cookie_path
        else:
            logger.warning("⚠️ No cookies found — YouTube may block requests")

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

    logger.info(
        "Starting download: %s | job_id=%s | user_id=%s",
        video_url,
        resolved_job_id,
        resolved_user_id,
    )

    try:
        ydl_opts = _build_ydl_opts()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)

            prepared_filename = ydl.prepare_filename(info)
            resolved_path = _resolve_downloaded_file(info, prepared_filename)

            if resolved_path is None:
                resolved_path = Path(prepared_filename)

            # 🔥 Ensure MP4 if possible
            if resolved_path.suffix.lower() != ".mp4":
                mp4_candidate = resolved_path.with_suffix(".mp4")
                if mp4_candidate.exists():
                    resolved_path = mp4_candidate

            absolute_file_path = resolved_path.resolve()

            return {
                "job_id": resolved_job_id,
                "message": "Download completed successfully",
                "status": "SUCCESS",
                "url": video_url,
                "user_id": resolved_user_id,
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "download_url": _build_download_url(absolute_file_path),
                "file_name": absolute_file_path.name,
                "file_path": str(absolute_file_path),
            }

    except DownloadError as e:
        logger.exception("yt-dlp download error")

        error_msg = str(e)

        # 🔥 Detect YouTube bot block
        if "Sign in to confirm you're not a bot" in error_msg:
            return {
                "job_id": resolved_job_id,
                "message": "YouTube is blocking this request (bot detection)",
                "status": "BLOCKED",
                "error": "youtube_bot_block",
                "url": video_url,
                "user_id": resolved_user_id,
            }

        return {
            "job_id": resolved_job_id,
            "message": "Download failed",
            "status": "FAILURE",
            "error": error_msg,
            "url": video_url,
            "user_id": resolved_user_id,
        }

    except Exception as e:
        logger.exception("Unexpected error")

        return {
            "job_id": resolved_job_id,
            "message": "Unexpected server error",
            "status": "ERROR",
            "error": str(e),
            "url": video_url,
            "user_id": resolved_user_id,
        }