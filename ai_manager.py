# ai_manager.py
"""
ai_manager.py

Refactored to strictly perform AI text-generation responsibilities only.
Contains only these public functions:

1. generate_caption(text_or_transcript, **kwargs) -> str
2. generate_title(text_or_transcript, **kwargs) -> str
3. generate_hashtags(text_or_transcript, max_hashtags=10, **kwargs) -> list[str]
4. generate_metadata(text_or_transcript, **kwargs) -> dict
5. analyze_content(text, **kwargs) -> dict
6. rewrite_text(text, style=None, preserve_length=False, **kwargs) -> str

No premium checks. No spend tracking required to operate. No queue logic. No file I/O.
The functions call OpenAI and return python-native types (strings, lists, dicts).
They try to produce JSON where appropriate and will fall back to raw text if parsing fails.

Usage: import ai_manager and call the functions. They will raise RuntimeError if the OpenAI key
is not present or if the underlying OpenAI call fails.
"""
from __future__ import annotations

import os
import json
from typing import Any, Dict, List, Optional

from logger_manager import log_info, log_warning, log_error

from dotenv import load_dotenv
import openai

# Optional spend-tracking integration (safe import)
try:
    from models import mark_ai_usage
except Exception:
    mark_ai_usage = None

# Load env at import (good for normal use). _ensure_api_key reads env dynamically.
load_dotenv()

# Default model settings (changeable via kwargs per-call)
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 300
DEFAULT_TEMPERATURE = 0.2

__all__ = [
    "generate_caption",
    "generate_title",
    "generate_hashtags",
    "generate_metadata",
    "analyze_content",
    "rewrite_text",
]


# ------------------------------
# Internal helpers
# ------------------------------

def _ensure_api_key():
    """Ensure an API key is available. Reads environment dynamically so restarts are not required."""
    key = os.getenv("OPENAI_API_KEY") or getattr(openai, "api_key", None)
    if not key:
        print("⚠️ OpenAI key not found. AI features disabled.")
    # ensure openai.api_key is set for SDK calls
    openai.api_key = key
    return key


def _extract_text_from_choice(choice) -> str:
    """
    Defensive extraction for different OpenAI SDK shapes:
    - new style: choice.message['content'] or choice.message.content
    - older style: choice.text
    - fallback: str(choice)
    """
    try:
        # try dict-like .message
        msg = getattr(choice, "message", None)
        if msg:
            # msg may be dict-like or object
            if isinstance(msg, dict):
                return msg.get("content", "").strip()
            # if it's an object with .get or .content
            content = getattr(msg, "get", None)
            if callable(content):
                return msg.get("content", "").strip()
            content_attr = getattr(msg, "content", None)
            if isinstance(content_attr, str):
                return content_attr.strip()
        # fallback to choice.text
        text = getattr(choice, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
    except Exception:
        pass
    # last fallback
    return str(choice)


def _call_openai(prompt: str, *, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS,
                 temperature: float = DEFAULT_TEMPERATURE) -> str:
    """Call the OpenAI chat completion endpoint and return the assistant text."""
    _ensure_api_key()

    try:
        completion = openai.ChatCompletion.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )

        # Try common response shapes
        if hasattr(completion, "choices") and completion.choices:
            choice = completion.choices[0]
            extracted = _extract_text_from_choice(choice)
            if extracted:
                return extracted

        # If no choices found, try to stringify the whole object
        return str(completion)
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e}")


def _parse_json_safe(text: str) -> Any:
    """Try to parse text as JSON. If parsing fails, return the raw text."""
    try:
        return json.loads(text)
    except Exception:
        return text


# ------------------------------
# Centralized logging wrapper
# ------------------------------

def _process_ai_request(user_id: Optional[str], operation: str, prompt: str,
                        *, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS,
                        temperature: float = DEFAULT_TEMPERATURE) -> str:
    """
    Central wrapper that logs AI requests and optionally records usage.
    - user_id: optional, may be None
    - operation: short label (e.g., "generate_caption")
    - prompt: the prompt text (we do NOT log full prompt content; only length)
    Returns the raw assistant text (string).
    """
    uid = user_id or "anonymous"
    try:
        log_info(f"[AIManager] {operation} requested by user={uid}; model={model}; prompt_len={len(prompt)}")
    except Exception:
        # Logging should never crash the flow
        pass

    try:
        result = _call_openai(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
        # best-effort usage recording: attempt to record tokens/usage if possible
        try:
            # We don't have exact token count here; approximate by characters->tokens heuristic (very rough)
            if mark_ai_usage and user_id:
                approx_tokens = max(1, len(prompt) // 4)  # crude approx: 4 chars ~ 1 token
                try:
                    mark_ai_usage(user_id, tokens_used=approx_tokens)
                    log_info(f"[AIManager] mark_ai_usage recorded for user={uid}; approx_tokens={approx_tokens}")
                except Exception as e:
                    log_warning(f"[AIManager] Failed to call mark_ai_usage for user={uid}: {e}")
        except Exception:
            # keep going if spend recording fails
            pass

        log_info(f"[AIManager] {operation} completed for user={uid}")
        return result
    except Exception as e:
        log_error(f"[AIManager] {operation} failed for user={uid}: {e}")
        raise


# ------------------------------
# Public API — text-generation only
# ------------------------------

def generate_caption(text_or_transcript: str, *, model: str = DEFAULT_MODEL, max_tokens: int = 120,
                     temperature: float = 0.3, user_id: Optional[str] = None) -> str:
    """Generate a short, engaging caption suitable for social platforms from text or transcript."""
    prompt = (
        "You are a helpful assistant that writes short social-media captions.\n"
        "Input: the transcript or text is delimited by triple backticks.\n"
        "Task: produce a concise, attention-grabbing caption (1-2 short sentences).\n"
        "Do NOT include hashtags. Do NOT include markdown or quotes.\n"
        "Respond with plain text only.\n\n"
        f"```{text_or_transcript}```"
    )
    return _process_ai_request(user_id, "generate_caption", prompt, model=model, max_tokens=max_tokens,
                               temperature=temperature)


def generate_title(text_or_transcript: str, *, model: str = DEFAULT_MODEL, max_tokens: int = 40,
                   temperature: float = 0.2, user_id: Optional[str] = None) -> str:
    """Generate a concise, clickable title (suitable for video or article)."""
    prompt = (
        "You are an expert copywriter who crafts concise, clickable titles.\n"
        "Input: the transcript or text is delimited by triple backticks.\n"
        "Task: produce 3 potential titles (each on its own line), ranked best to worst."
        " Then append a single-line comment selecting the best one (prefix: BEST:).\n"
        "Respond with plain text only.\n\n"
        f"```{text_or_transcript}```"
    )
    response = _process_ai_request(user_id, "generate_title", prompt, model=model, max_tokens=max_tokens,
                                   temperature=temperature)
    return response


def generate_hashtags(text_or_transcript: str, *, max_hashtags: int = 10, model: str = DEFAULT_MODEL,
                      max_tokens: int = 120, temperature: float = 0.3, user_id: Optional[str] = None) -> List[str]:
    """Generate a list of relevant hashtags."""
    prompt = (
        "You are a hashtag generator.\n"
        "Input: text or transcript is delimited by triple backticks.\n"
        "Task: suggest up to {max_h} concise hashtags relevant to the content.\n"
        "Return the result as a JSON array of strings only.\n\n"
        f"```{text_or_transcript}```"
    ).replace("{max_h}", str(max_hashtags))

    response = _process_ai_request(user_id, "generate_hashtags", prompt, model=model, max_tokens=max_tokens,
                                   temperature=temperature)
    parsed = _parse_json_safe(response)

    if isinstance(parsed, list):
        cleaned = []
        for item in parsed[:max_hashtags]:
            s = str(item).strip()
            if not s:
                continue
            if not s.startswith("#"):
                s = "#" + s.replace(" ", "")
            cleaned.append(s)
        return cleaned

    # Fallback: try to extract hashtags from raw text
    raw = response.replace("\n", " ").split()
    hashtags = [w for w in raw if w.startswith("#")]
    if hashtags:
        return hashtags[:max_hashtags]

    # Very last fallback: return a tiny set of keywords as hashtags
    words = [w.strip(".,:;!?") for w in text_or_transcript.split()]
    top = list(dict.fromkeys(words))[:max_hashtags]
    return [("#" + w) for w in top if w]


def generate_metadata(text_or_transcript: str, *, model: str = DEFAULT_MODEL, max_tokens: int = 250,
                      temperature: float = 0.2, user_id: Optional[str] = None) -> Dict[str, Any]:
    """Generate structured metadata from the text."""
    prompt = (
        "You are an assistant that extracts metadata from content.\n"
        "Input: the transcript or text is delimited by triple backticks.\n"
        "Task: produce a JSON object with the following keys: 'description' (1-2 sentences),"
        " 'language' (ISO or simple name), 'keywords' (array of short keyword strings),"
        " 'estimated_reading_time' (integer minutes), and 'tone' (short).\n"
        "Respond with valid JSON only.\n\n"
        f"```{text_or_transcript}```"
    )

    response = _process_ai_request(user_id, "generate_metadata", prompt, model=model, max_tokens=max_tokens,
                                   temperature=temperature)
    parsed = _parse_json_safe(response)

    if isinstance(parsed, dict):
        return parsed

    description = response.split("\n")[0][:200]
    return {
        "description": description,
        "language": "unknown",
        "keywords": [],
        "estimated_reading_time": max(1, len(text_or_transcript.split()) // 200),
        "tone": "unknown",
        "raw": response,
    }


def analyze_content(text: str, *, model: str = DEFAULT_MODEL, max_tokens: int = 300,
                    temperature: float = 0.0, user_id: Optional[str] = None) -> Dict[str, Any]:
    """Analyze content and return insights such as sentiment, topics, safety flags, and suggested actions."""
    prompt = (
        "You are a content analyst.\n"
        "Input: the text is delimited by triple backticks.\n"
        "Task: analyze the content and return a JSON object with keys: 'summary' (1-2 sentences),"
        " 'sentiment' (one of: positive, neutral, negative), 'topics' (array of short topics),"
        " 'language', and 'is_sensitive' (true/false). Provide valid JSON only.\n\n"
        f"```{text}```"
    )

    response = _process_ai_request(user_id, "analyze_content", prompt, model=model, max_tokens=max_tokens,
                                   temperature=temperature)
    parsed = _parse_json_safe(response)

    if isinstance(parsed, dict):
        return parsed

    return {"raw": response}


def rewrite_text(text: str, *, style: Optional[str] = None, preserve_length: bool = False,
                 model: str = DEFAULT_MODEL, max_tokens: int = 400, temperature: float = 0.25,
                 user_id: Optional[str] = None) -> str:
    """Rewrite the given text according to an optional style."""
    style_line = f"Style: {style}." if style else "Keep the original meaning."
    length_hint = "Preserve the original length." if preserve_length else "Length can be adjusted as needed."

    prompt = (
        "You are an assistant that rewrites text while keeping original meaning.\n"
        "Input: the text is delimited by triple backticks.\n"
        f"{style_line} {length_hint}\n"
        "Respond with the rewritten text only (no commentary).\n\n"
        f"```{text}```"
    )

    return _process_ai_request(user_id, "rewrite_text", prompt, model=model, max_tokens=max_tokens,
                               temperature=temperature)


# End of ai_manager.py

if __name__ == "__main__":
    # quick local test (safe if key missing)
    try:
        print("Testing generate_caption() with sample input...")
        sample = "A luxury sports car on a sunny day showing speed, chrome details, and a scenic coastal road."
        caption = generate_caption(sample)
        print("Caption result:")
        print(caption)
    except RuntimeError as e:
        print("ai_manager quick test skipped: " + str(e))
        print("Make sure your .env contains OPENAI_API_KEY=sk-... and then re-run:")
        print("    python ai_manager.py")
