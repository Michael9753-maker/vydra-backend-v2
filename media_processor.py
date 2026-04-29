"""
media_processor.py

VYDRA media processor — local, non-AI media utilities.

Responsibilities implemented:
- transcode_video: ensure output is MP4 (h264/aac)
- upscale_video: upscale using ffmpeg scaling
- extract_audio_premium: premium-only high-quality extraction (m4a) + optional enhancement
- enhance_audio: lightweight "smart" enhancement (afftdn + loudnorm + EQ)
- enhance_video: small local filters (denoise/sharpen/brighten)
- generate_thumbnail: capture a frame
- get_media_info: ffprobe JSON
- process_after_download: convenience wrapper (premium-only audio extraction)

Notes:
- Blocking, uses subprocess to call ffmpeg/ffprobe. Caller should run in worker thread/process when used in web request handling.
- extract_audio (basic) is intentionally NOT provided here — basic extraction for free/guest users must be implemented in download_manager.py as `extract_audio_basic` to avoid mixing premium logic with free features.
- This module is intended for premium/advanced processing only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("media_processor")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Configuration
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "downloads")).resolve()
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_FFMPEG = shutil.which("ffmpeg")
DEFAULT_FFPROBE = shutil.which("ffprobe")

UPSCALE_PRESETS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
}

# -------------------------
# Internal helpers
# -------------------------

def _ffmpeg_path() -> str:
    if DEFAULT_FFMPEG:
        return DEFAULT_FFMPEG
    raise RuntimeError("ffmpeg not found in PATH")


def _ffprobe_path() -> str:
    if DEFAULT_FFPROBE:
        return DEFAULT_FFPROBE
    raise RuntimeError("ffprobe not found in PATH")


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    """Run a subprocess command and return (rc, stdout, stderr)."""
    logger.debug("Running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        logger.error("Command timeout: %s", e)
        return 124, "", str(e)
    except Exception as e:
        logger.exception("Command failed: %s", e)
        return 1, "", str(e)


def _ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _safe_output(base: str, suffix: str) -> str:
    p = Path(base)
    return str(p.with_name(p.stem + suffix))


# -------------------------
# Media info
# -------------------------

def get_media_info(path: str) -> Dict[str, Any]:
    """Return ffprobe JSON info or empty dict on failure."""
    try:
        ffprobe = _ffprobe_path()
        cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path]
        rc, out, err = _run(cmd)
        if rc == 0 and out:
            return json.loads(out)
        logger.debug("ffprobe failed: %s", err.strip())
    except Exception:
        logger.exception("get_media_info failed")
    return {}


# -------------------------
# Thumbnail
# -------------------------

def generate_thumbnail(video_path: str, output_path: Optional[str] = None, time_position: float = 1.0) -> str:
    ffmpeg = _ffmpeg_path()
    if not output_path:
        output_path = _safe_output(video_path, "_thumb.jpg")
    _ensure_parent(output_path)
    cmd = [ffmpeg, "-y", "-ss", str(time_position), "-i", video_path, "-vframes", "1", "-q:v", "2", output_path]
    rc, out, err = _run(cmd)
    if rc != 0 or not Path(output_path).exists():
        raise RuntimeError(f"thumbnail extraction failed: {err.strip()}")
    return output_path


# -------------------------
# Premium audio extraction & enhancement
# -------------------------

def extract_audio_premium(video_path: str, output_path: Optional[str] = None, fmt: str = "m4a", enhance: bool = False) -> str:
    """Premium-only audio extraction.

    - Encodes audio to a high-quality m4a (AAC) by default.
    - If `enhance` is True, runs enhance_audio on the produced file and returns the enhanced path.

    This function intentionally replaces the older generic `extract_audio` to keep basic extraction
    inside download_manager.py for free/guest users.
    """
    ffmpeg = _ffmpeg_path()
    base = Path(video_path)
    if not output_path:
        output_path = _safe_output(video_path, f"_audio.{fmt}")
    _ensure_parent(output_path)

    # Re-encode to AAC inside .m4a for premium quality
    cmd = [ffmpeg, "-y", "-i", video_path, "-vn", "-c:a", "aac", "-b:a", "256k", "-ar", "48000", output_path]
    rc, out, err = _run(cmd)
    if rc != 0 or not Path(output_path).exists():
        raise RuntimeError(f"premium audio extraction failed: {err.strip()}")

    if enhance:
        # produce enhanced file path
        try:
            enhanced = enhance_audio(output_path)
            return enhanced
        except Exception as e:
            # if enhancement fails, return the original extracted file but log warning
            logger.warning("audio enhancement failed after extraction: %s", e)
            return output_path

    return output_path


def enhance_audio(audio_path: str, out_path: Optional[str] = None, method: str = "smart") -> str:
    """Lightweight local (non-AI) audio enhancement.

    method 'smart' -> afftdn + loudnorm + mild eq.
    Returns the path to the enhanced file.
    """
    ffmpeg = _ffmpeg_path()
    base = Path(audio_path)
    if not out_path:
        out_path = str(base.with_name(base.stem + "_enhanced" + base.suffix or ".wav"))
    _ensure_parent(out_path)

    filters: List[str] = []
    if method == "smart":
        # afftdn is available in modern ffmpeg builds, fall back to anull if not supported at runtime
        filters.append("afftdn")
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        filters.append("equalizer=f=3000:t=q:w=1:g=1.5")

    filter_chain = ",".join(filters) if filters else "anull"
    cmd = [ffmpeg, "-y", "-i", audio_path, "-af", filter_chain, "-ar", "44100", "-ac", "2", out_path]
    rc, out, err = _run(cmd)
    if rc != 0 or not Path(out_path).exists():
        # If afftdn/equalizer missing or failed, attempt a safer fallback: apply loudnorm only
        logger.warning("audio enhance primary failed: %s. Attempting fallback loudnorm only.", err.strip())
        fallback = [ffmpeg, "-y", "-i", audio_path, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "2", out_path]
        rc2, out2, err2 = _run(fallback)
        if rc2 != 0 or not Path(out_path).exists():
            raise RuntimeError(f"audio enhancement failed: {err2.strip() or err.strip()}")
    return out_path


# -------------------------
# Video transcode & upscale
# -------------------------

def _build_hwaccel_args(hwaccel: Optional[str]) -> List[str]:
    # For now, return encoder selection via -c:v. More advanced setups may require -vaapi_device etc.
    if not hwaccel:
        return []
    if hwaccel == "nvenc":
        return ["-c:v", "h264_nvenc"]
    if hwaccel == "qsv":
        return ["-c:v", "h264_qsv"]
    # vaapi typically needs additional device flags; default to software encode
    return []


def transcode_video(input_path: str, output_path: Optional[str] = None, target_codec: str = "libx264",
                    preset: str = "fast", crf: int = 23, hwaccel: Optional[str] = None) -> str:
    ffmpeg = _ffmpeg_path()
    base = Path(input_path)
    if not output_path:
        output_path = str(base.with_name(base.stem + "_transcoded.mp4"))
    _ensure_parent(output_path)

    # Fast-path: try to container-copy if compatible
    info = get_media_info(input_path)
    try:
        streams = info.get("streams", [])
        vcodec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None)
        acodec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)
        if vcodec in ("h264", "hevc") and acodec in ("aac", "mp3"):
            cmd = [ffmpeg, "-y", "-i", input_path, "-c", "copy", output_path]
            rc, out, err = _run(cmd)
            if rc == 0 and Path(output_path).exists():
                return output_path
    except Exception:
        logger.debug("fast-path transcode check failed; falling back to full encode")

    hw_args = _build_hwaccel_args(hwaccel)
    encode_args: List[str] = []
    if hw_args:
        encode_args = hw_args + ["-preset", preset]
    else:
        encode_args = ["-c:v", target_codec, "-preset", preset, "-crf", str(crf)]

    audio_args = ["-c:a", "aac", "-b:a", "192k"]
    cmd = [ffmpeg, "-y", "-i", input_path] + encode_args + audio_args + [output_path]
    rc, out, err = _run(cmd)
    if rc != 0 or not Path(output_path).exists():
        raise RuntimeError(f"transcode failed: {err.strip()}")
    return output_path


def upscale_video(input_path: str, target: str = "1080p", output_path: Optional[str] = None, hwaccel: Optional[str] = None, preset: str = "fast") -> str:
    if target not in UPSCALE_PRESETS:
        raise ValueError(f"unsupported upscale target: {target}")
    w, h = UPSCALE_PRESETS[target]
    ffmpeg = _ffmpeg_path()
    base = Path(input_path)
    if not output_path:
        output_path = str(base.with_name(f"{base.stem}_{target}.mp4"))
    _ensure_parent(output_path)

    vf = f"scale={w}:{h}:flags=lanczos,unsharp=5:5:0.8:3:3:0.4"
    # For simplicity, we use libx264 encode
    cmd = [ffmpeg, "-y", "-i", input_path, "-vf", vf, "-c:v", "libx264", "-preset", preset, "-crf", "20", "-c:a", "copy", output_path]
    rc, out, err = _run(cmd)
    if rc != 0 or not Path(output_path).exists():
        raise RuntimeError(f"upscale failed: {err.strip()}")
    return output_path


# -------------------------
# Video enhancement filters
# -------------------------

def enhance_video(input_path: str, output_path: Optional[str] = None, denoise: bool = False, sharpen: bool = False, brighten: bool = False) -> str:
    ffmpeg = _ffmpeg_path()
    base = Path(input_path)
    if not output_path:
        output_path = str(base.with_name(base.stem + "_v_enhanced.mp4"))
    _ensure_parent(output_path)

    filters: List[str] = []
    if denoise:
        filters.append("hqdn3d")
    if sharpen:
        filters.append("unsharp=5:5:0.8:3:3:0.4")
    if brighten:
        filters.append("eq=brightness=0.03:saturation=1.05")

    vf = ",".join(filters) if filters else "null"
    cmd = [ffmpeg, "-y", "-i", input_path, "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "copy", output_path]
    rc, out, err = _run(cmd)
    if rc != 0 or not Path(output_path).exists():
        raise RuntimeError(f"video enhancement failed: {err.strip()}")
    return output_path


# -------------------------
# Convenience wrapper
# -------------------------

def process_after_download(filepath: str, *, generate_thumb: bool = False, extract_audio_only: bool = False, transcode_to: Optional[str] = None, upscale_to: Optional[str] = None, smart_audio: bool = False) -> Dict[str, Any]:
    """Wrapper used by backend. Caller enforces permissions/premium.

    Returns a dict with keys for produced artifacts or errors.
    NOTE: basic (free) audio extraction is NOT performed here. This wrapper will only
    perform premium audio extraction when smart_audio=True. For basic extraction use
    `download_manager.extract_audio_basic()`.
    """
    results: Dict[str, Any] = {}
    if generate_thumb:
        try:
            results["thumbnail"] = generate_thumbnail(filepath)
        except Exception as e:
            results["thumbnail_error"] = str(e)

    if extract_audio_only:
        try:
            if smart_audio:
                a = extract_audio_premium(filepath, enhance=True)
                results["audio"] = a
                results["audio_enhanced"] = a
            else:
                results["audio_error"] = "Basic audio extraction is handled by download_manager. Use download_manager.extract_audio_basic for free users."
        except Exception as e:
            results["audio_error"] = str(e)

    if transcode_to:
        try:
            results["transcoded"] = transcode_video(filepath)
        except Exception as e:
            results["transcode_error"] = str(e)

    if upscale_to:
        try:
            results["upscaled"] = upscale_video(filepath, target=upscale_to)
        except Exception as e:
            results["upscale_error"] = str(e)

    return results


# If invoked directly, provide a small CLI for testing
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="media_processor quick test CLI")
    p.add_argument("file", help="input media file")
    p.add_argument("--thumbnail", action="store_true")
    p.add_argument("--extract-audio", action="store_true")
    p.add_argument("--enhance-audio", action="store_true")
    p.add_argument("--transcode", action="store_true")
    p.add_argument("--upscale", choices=list(UPSCALE_PRESETS.keys()))
    args = p.parse_args()

    fp = args.file
    out = process_after_download(fp, generate_thumb=args.thumbnail, extract_audio_only=args.extract_audio, transcode_to=("mp4" if args.transcode else None), upscale_to=args.upscale, smart_audio=args.enhance_audio)
    print(json.dumps(out, indent=2))
