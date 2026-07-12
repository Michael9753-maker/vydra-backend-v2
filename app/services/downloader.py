from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import quote

import yt_dlp
from dotenv import load_dotenv
from yt_dlp.utils import DownloadError

load_dotenv()

logger = logging.getLogger(__name__)

# Base paths
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_PATH = Path(os.getenv("DOWNLOADS_DIR", str(DEFAULT_DOWNLOAD_DIR))).expanduser().resolve()
DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)

DOWNLOAD_URL_PREFIX = "/api/download/file/"

# Unified cookie file for all supported platforms.
DEFAULT_COOKIE_FILE = BASE_DIR / "cookies.txt"
COOKIE_FILE = Path(
    os.getenv("YTDLP_COOKIE_FILE")
    or os.getenv("YTDLP_COOKIEFILE")
    or str(DEFAULT_COOKIE_FILE)
).expanduser().resolve()

# Optional fallback for injecting cookies through env/secret managers.
COOKIES_CONTENT = os.getenv("YTDLP_COOKIES_CONTENT", "").strip()
RUNTIME_COOKIE_FILE = Path(
    os.getenv(
        "YTDLP_RUNTIME_COOKIE_FILE",
        str(BASE_DIR / "cookies.runtime.txt"),
    )
).expanduser().resolve()

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

TIKTOK_REFERER = "https://www.tiktok.com/"

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

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


def clean_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|#]', "", str(title or ""))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:100]


def _clean_error_message(message: Any) -> str:
    text = str(message or "").strip()
    return ANSI_ESCAPE_RE.sub("", text).strip()


def _host_from_url(url: str) -> str:
    try:
        return re.sub(r"^www\.", "", str(url).split("//", 1)[-1].split("/", 1)[0].lower())
    except Exception:
        return ""


def _is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in _host_from_url(url)


def _pick_url(url: Optional[str] = None, **kwargs) -> str:
    candidate = url or kwargs.get("video_url") or kwargs.get("source_url") or kwargs.get("link")
    if not candidate:
        raise ValueError("url is required")
    return str(candidate).strip()


def _pick_user_id(user_id: Optional[str] = None, **kwargs) -> str:
    return str(user_id or kwargs.get("user_id") or "").strip()


def _pick_job_id(job_id: Optional[str] = None, **kwargs) -> str:
    return str(job_id or kwargs.get("job_id") or "").strip()


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
      1) cookies.txt in project root (recommended)
      2) runtime cookies from YTDLP_COOKIES_CONTENT
    """
    if COOKIE_FILE.exists() and COOKIE_FILE.is_file():
        logger.info("Using cookie file: %s", COOKIE_FILE)
        return str(COOKIE_FILE.resolve())

    if COOKIES_CONTENT:
        try:
            RUNTIME_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            RUNTIME_COOKIE_FILE.write_text(COOKIES_CONTENT, encoding="utf-8")
            logger.info("Created runtime cookie file: %s", str(RUNTIME_COOKIE_FILE))
            return str(RUNTIME_COOKIE_FILE.resolve())
        except Exception as exc:
            logger.error("Failed to write runtime cookie file: %s", exc)

    logger.warning("No cookies found; extraction may be blocked on some sites")
    return None


def _build_ydl_opts(
    cookie_path: Optional[str] = None,
    user_agent: str = DESKTOP_USER_AGENT,
    referer: Optional[str] = None,
) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "outtmpl": str(DOWNLOAD_PATH / "%(extractor)s_%(id)s.%(ext)s"),
        "noplaylist": True,
        "extractor_retries": 3,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 4,
        "force_ipv4": True,
        "continuedl": True,
        "overwrites": True,
        "ignoreerrors": False,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if referer:
        opts["http_headers"]["Referer"] = referer

    if cookie_path:
        opts["cookiefile"] = cookie_path

    return opts


def _build_download_attempts(video_url: str) -> list[Tuple[Optional[str], str, Optional[str]]]:
    """
    Strategy:
      - Use cookies first when available
      - Desktop UA first, then mobile UA
      - TikTok gets a referer boost
    """
    cookie_path = _prepare_cookie_file()
    attempts: list[Tuple[Optional[str], str, Optional[str]]] = []

    if cookie_path:
        if _is_tiktok_url(video_url):
            attempts.append((cookie_path, DESKTOP_USER_AGENT, TIKTOK_REFERER))
            attempts.append((cookie_path, MOBILE_USER_AGENT, TIKTOK_REFERER))
        else:
            attempts.append((cookie_path, DESKTOP_USER_AGENT, None))
            attempts.append((cookie_path, MOBILE_USER_AGENT, None))

    attempts.append((None, DESKTOP_USER_AGENT, None))
    attempts.append((None, MOBILE_USER_AGENT, None))

    deduped: list[Tuple[Optional[str], str, Optional[str]]] = []
    seen = set()
    for item in attempts:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    return deduped


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
        prepared = Path(prepared_filename)
        candidates.append(prepared)

        stem = prepared.with_suffix("")
        for ext in POSSIBLE_EXTENSIONS:
            candidates.append(stem.with_suffix(f".{ext}"))

    existing = list(_iter_existing(candidates))
    if existing:
        for candidate in existing:
            if candidate.suffix.lower() == ".mp4":
                return candidate
        return existing[0]

    return None


def _build_download_url(file_path: Path) -> str:
    base_url = os.getenv("BASE_URL", "").rstrip("/")
    return f"{base_url}{DOWNLOAD_URL_PREFIX}{quote(file_path.name)}"


def _download_once(
    video_url: str,
    cookie_path: Optional[str],
    user_agent: str,
    referer: Optional[str],
):
    opts = _build_ydl_opts(cookie_path=cookie_path, user_agent=user_agent, referer=referer)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        filename = ydl.prepare_filename(info)

        file_path = _resolve_downloaded_file(info, filename)
        if not file_path:
            raise Exception("File not found after download")

        return info, file_path.resolve()


def process_download(
    url: Optional[str] = None,
    user_id: Optional[str] = None,
    job_id: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    video_url = _pick_url(url, **kwargs)
    resolved_user_id = _pick_user_id(user_id, **kwargs)
    resolved_job_id = _pick_job_id(job_id, **kwargs)

    logger.info("Download start: %s", video_url)

    attempts = _build_download_attempts(video_url)
    last_error = "Download failed"
    debug_log: list[Dict[str, Any]] = []

    for i, (cookie_path, ua, ref) in enumerate(attempts, 1):
        attempt_info: Dict[str, Any] = {
            "attempt": i,
            "cookies": "yes" if cookie_path else "no",
            "cookie_mode": "cookiefile" if cookie_path else "none",
            "user_agent": "desktop" if ua == DESKTOP_USER_AGENT else "mobile",
            "referer": ref,
            "status": "pending",
            "error": None,
        }

        try:
            logger.info(
                "Attempt %s/%s | cookies=%s | ua=%s",
                i,
                len(attempts),
                attempt_info["cookies"],
                attempt_info["user_agent"],
            )

            info, file_path = _download_once(video_url, cookie_path, ua, ref)
            title = info.get("title") or file_path.stem

            attempt_info["status"] = "success"
            attempt_info["resolved_file"] = str(file_path)
            debug_log.append(attempt_info)

            return {
                "success": True,
                "status": "SUCCESS",
                "message": "Download completed",
                "download_url": _build_download_url(file_path),
                "file_name": file_path.name,
                "file_path": str(file_path),
                "job_id": resolved_job_id,
                "user_id": resolved_user_id,
                "title": title,
                "clean_title": clean_filename(title),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "ext": info.get("ext"),
                "webpage_url": info.get("webpage_url") or video_url,
                "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
                "extractor": info.get("extractor"),
                "availability": info.get("availability"),
                "id": info.get("id"),
                "debug": debug_log,
            }

        except DownloadError as e:
            last_error = _clean_error_message(e)
            attempt_info["status"] = "failed"
            attempt_info["error"] = last_error
            debug_log.append(attempt_info)
            logger.warning("Attempt %s failed: %s", i, last_error)

        except Exception as e:
            last_error = _clean_error_message(e)
            attempt_info["status"] = "failed"
            attempt_info["error"] = last_error
            debug_log.append(attempt_info)
            logger.warning("Attempt %s failed: %s", i, last_error)

    return {
        "success": False,
        "status": "FAILURE",
        "message": last_error,
        "download_url": None,
        "file_name": "",
        "file_path": "",
        "job_id": resolved_job_id,
        "user_id": resolved_user_id,
        "url": video_url,
        "debug": debug_log,
    }
