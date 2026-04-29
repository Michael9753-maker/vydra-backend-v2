from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

import yt_dlp

logger = logging.getLogger(__name__)

# Use one stable download directory for the whole backend
DEFAULT_DOWNLOAD_DIR = Path(__file__).resolve().parents[2] / "downloads"
DOWNLOAD_PATH = Path(os.getenv("DOWNLOADS_DIR", str(DEFAULT_DOWNLOAD_DIR))).expanduser().resolve()
DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)

DOWNLOAD_URL_PREFIX = "/api/download/file/"

# Common extensions yt-dlp may leave behind after post-processing
POSSIBLE_EXTENSIONS = (
    "mp4",
    "mkv",
    "webm",
    "mov",
    "m4v",
    "flv",
    "avi",
    "mp3",
    "m4a",
    "aac",
    "opus",
    "ts",
)

# Cookie support:
# 1) By default, look for cookies.txt in the backend root:
#    C:\Users\PC\vydra-backend\cookies.txt
# 2) You can override with YTDLP_COOKIEFILE
# 3) You can also use browser cookies via YTDLP_COOKIES_FROM_BROWSER
DEFAULT_COOKIE_FILE = Path(__file__).resolve().parents[2] / "cookies.txt"
COOKIE_FILE = os.getenv("YTDLP_COOKIEFILE", str(DEFAULT_COOKIE_FILE)).strip()
COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()


def clean_filename(title: str) -> str:
    """
    Minimal cleanup for display and fallback matching.
    We avoid long titles in the actual output filename to prevent path issues.
    """
    title = re.sub(r'[\\/*?:"<>|#]', "", str(title or ""))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:100]


def _pick_url(url: Optional[str] = None, **kwargs) -> str:
    candidate = url or kwargs.get("video_url") or kwargs.get("source_url") or kwargs.get("link")
    if not candidate:
        raise ValueError("url is required")
    return str(candidate).strip()


def _pick_user_id(user_id: Optional[str] = None, **kwargs) -> str:
    candidate = user_id or kwargs.get("user_id") or kwargs.get("email") or kwargs.get("user_email")
    return str(candidate).strip() if candidate is not None else ""


def _iter_existing(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        try:
            if p.exists() and p.is_file():
                yield p
        except Exception:
            continue


def _safe_token(value: str) -> str:
    return clean_filename(str(value or "")).replace(" ", "_").lower()


def _build_ydl_opts(use_cookies: bool = False) -> Dict[str, Any]:
    """
    Build yt-dlp options.

    Important choices:
    - Short output filename based on extractor + id to avoid long-path failures
    - No automatic Chrome database copy unless explicitly requested
    - Safe retry settings for unstable social platforms
    """
    opts: Dict[str, Any] = {
        "outtmpl": str(DOWNLOAD_PATH / "%(extractor)s_%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "continuedl": True,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "skip_download": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
    }

    if use_cookies:
        cookiefile = Path(COOKIE_FILE).expanduser()
        if COOKIE_FILE and cookiefile.exists() and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        elif COOKIES_FROM_BROWSER:
            # Examples:
            #   YTDLP_COOKIES_FROM_BROWSER=chrome
            #   YTDLP_COOKIES_FROM_BROWSER=firefox
            #   YTDLP_COOKIES_FROM_BROWSER=chrome:Default
            if ":" in COOKIES_FROM_BROWSER:
                browser, profile = COOKIES_FROM_BROWSER.split(":", 1)
                opts["cookiesfrombrowser"] = (browser.strip(), profile.strip())
            else:
                opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER.strip(),)

    return opts


def _resolve_downloaded_file(info: Dict[str, Any], prepared_filename: str) -> Optional[Path]:
    """
    Try to find the actual file yt-dlp wrote to disk.

    This is defensive because different platforms and post-processors may
    leave different final filenames behind.
    """
    candidates: list[Path] = []

    # 1) Anything yt-dlp already exposes as a direct path
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

    # 2) Prepared filename
    prepared_path = Path(prepared_filename)
    candidates.append(prepared_path)

    # 3) Common post-processed variants
    stem = prepared_path.with_suffix("")
    for ext in POSSIBLE_EXTENSIONS:
        candidates.append(stem.with_suffix(f".{ext}"))

    # Return the first real file
    existing = list(_iter_existing(candidates))
    if existing:
        # Prefer mp4 if present
        for candidate in existing:
            if candidate.suffix.lower() == ".mp4":
                return candidate
        return existing[0]

    # 4) Search the downloads directory using id/title tokens
    search_tokens = []
    video_id = str(info.get("id") or "").strip()
    title = str(info.get("title") or "").strip()
    extractor = str(info.get("extractor") or info.get("extractor_key") or "").strip()

    for token in (video_id, title, extractor, prepared_path.stem):
        token = _safe_token(token)
        if token:
            search_tokens.append(token)

    for token in search_tokens:
        matches = []
        try:
            for item in DOWNLOAD_PATH.iterdir():
                if not item.is_file():
                    continue
                if item.name.endswith(".part") or item.name.endswith(".ytdl"):
                    continue

                name_lower = item.name.lower()
                if token in name_lower:
                    matches.append(item)
        except Exception:
            continue

        if matches:
            matches.sort(
                key=lambda p: (
                    0 if p.suffix.lower() == ".mp4" else 1,
                    -p.stat().st_mtime if p.exists() else 0,
                )
            )
            return matches[0]

    return None


def _build_download_url(file_path: Path) -> str:
    return f"{DOWNLOAD_URL_PREFIX}{quote(file_path.name)}"


def process_download(
    url: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Real download engine for VYDRA.

    Supports:
    - process_download(url, user_id)
    - process_download(url=<...>, user_id=<...>)
    - process_download(video_url=<...>, user_id=<...>)
    - process_download(source_url=<...>, user_id=<...>)
    """
    video_url = _pick_url(url, **kwargs)
    resolved_user_id = _pick_user_id(user_id, **kwargs)

    # Try without cookies first.
    # If cookies are available, retry with cookies on the second pass.
    attempts = [False]
    cookie_path = Path(COOKIE_FILE).expanduser()
    if (COOKIE_FILE and cookie_path.exists() and cookie_path.is_file()) or COOKIES_FROM_BROWSER:
        attempts.append(True)

    last_error: Optional[Exception] = None

    for use_cookies in attempts:
        ydl_opts = _build_ydl_opts(use_cookies=use_cookies)
        logger.info(
            "Starting yt-dlp download: %s (cookies=%s)",
            video_url,
            "on" if use_cookies else "off",
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)

                prepared_filename = ydl.prepare_filename(info)
                resolved_path = _resolve_downloaded_file(info, prepared_filename)

                if resolved_path is None:
                    resolved_path = Path(prepared_filename)

                # Prefer merged MP4 when available
                if resolved_path.suffix.lower() != ".mp4":
                    mp4_candidate = resolved_path.with_suffix(".mp4")
                    if mp4_candidate.exists() and mp4_candidate.is_file():
                        resolved_path = mp4_candidate

                absolute_file_path = resolved_path.resolve()

                result = {
                    "message": "Download completed successfully",
                    "status": "completed",
                    "url": video_url,
                    "user_id": resolved_user_id,
                    "title": info.get("title"),
                    "thumbnail": info.get("thumbnail"),
                    "webpage_url": info.get("webpage_url", video_url),
                    "ext": info.get("ext"),
                    "file_path": str(absolute_file_path),
                    "download_url": _build_download_url(absolute_file_path),
                    "file_name": absolute_file_path.name,
                    "platform": info.get("extractor_key") or info.get("extractor"),
                }

                try:
                    result["file_size"] = absolute_file_path.stat().st_size
                except Exception:
                    pass

                logger.info("Download finished successfully: %s", result["file_path"])
                return result

        except Exception as exc:
            last_error = exc
            logger.warning("yt-dlp attempt failed for %s: %s", video_url, exc)

    logger.exception("Download failed for %s", video_url)
    return {
        "message": "Download failed",
        "status": "failed",
        "error": str(last_error) if last_error else "unknown error",
        "url": video_url,
        "user_id": resolved_user_id,
    }