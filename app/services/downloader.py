from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

import yt_dlp

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
COOKIE_FILE = os.getenv("YTDLP_COOKIEFILE", str(DEFAULT_COOKIE_FILE)).strip()
COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()


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


# 📂 File helpers
def _iter_existing(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        try:
            if p.exists() and p.is_file():
                yield p
        except Exception:
            continue


def _build_ydl_opts() -> Dict[str, Any]:
    """
    ⚡ Optimized yt-dlp config for speed + stability (no Celery mode)
    """
    return {
        # 📁 Output
        "outtmpl": str(DOWNLOAD_PATH / "%(extractor)s_%(id)s.%(ext)s"),

        # ⚡ FAST FORMAT (no merging when possible)
        "format": "mp4/best[ext=mp4]/best",

        "merge_output_format": "mp4",
        "noplaylist": True,

        # 🔇 Silent = faster
        "quiet": True,
        "no_warnings": True,

        # 🌐 Network tuning
        "socket_timeout": 10,
        "retries": 1,
        "fragment_retries": 1,
        "concurrent_fragment_downloads": 4,

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
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        },

        # ⚡ Faster YouTube extraction
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
            }
        },
    }


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
    return f"{DOWNLOAD_URL_PREFIX}{quote(file_path.name)}"


# 🚀 MAIN FUNCTION
def process_download(
    url: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:

    video_url = _pick_url(url, **kwargs)
    resolved_user_id = _pick_user_id(user_id, **kwargs)

    logger.info(f"Starting download: {video_url}")

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

    except Exception as e:
        logger.exception("Download failed")

        return {
            "message": "Download failed",
            "status": "FAILURE",
            "error": str(e),
            "url": video_url,
            "user_id": resolved_user_id,
        }