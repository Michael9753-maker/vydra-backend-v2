# error_handler.py
"""
Centralized error types and helpers for VYDRA.

This module intentionally avoids performing Flask imports at import-time so it's safe
to import from anywhere. To hook into Flask, call `register_flask_handlers(app)` during
app initialization.

Public API:
- Exceptions: VydraError, BadRequestError, NotFoundError, UnauthorizedError, ExternalServiceError
- format_error_response(exc, include_trace=False) -> (payload_dict, http_status)
- log_exception(exc, context=None)
- handle_exceptions(default_status=500) -> decorator
- register_flask_handlers(app)
"""

from __future__ import annotations
import traceback
import logging
from typing import Optional, Any, Tuple, Dict, Callable

# Try to read config lazily so importing this module early won't require config to be wired
def _debug_mode() -> bool:
    try:
        from config import get_config
        cfg = get_config()
        return bool(getattr(cfg, "DEBUG", False))
    except Exception:
        return False

# Basic logger for this module
_logger = logging.getLogger("vydra.error_handler")
if not _logger.handlers:
    # if no handlers configured, add a simple stream handler for safety
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _logger.addHandler(ch)
    _logger.setLevel(logging.INFO)

# -------------------------
# Custom exceptions
# -------------------------
class VydraError(Exception):
    """Base exception for VYDRA-specific errors."""
    pass

class BadRequestError(VydraError):
    """Client sent invalid data or parameters (HTTP 400)."""
    pass

class NotFoundError(VydraError):
    """Requested resource not found (HTTP 404)."""
    pass

class UnauthorizedError(VydraError):
    """Authentication/authorization failure (HTTP 401/403)."""
    pass

class ExternalServiceError(VydraError):
    """An external dependency (yt-dlp, payment gateway, etc.) failed."""
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code

# -------------------------
# Formatting & logging
# -------------------------
def _build_traceback(exc: BaseException) -> str:
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        return str(exc)

def format_error_response(exc: BaseException, include_trace: Optional[bool] = None) -> Tuple[Dict[str, Any], int]:
    """
    Format an exception into (payload_dict, http_status).
    include_trace: if True, include 'trace' in payload. If None, controlled by config DEBUG.
    """
    if include_trace is None:
        include_trace = _debug_mode()

    # Default mapping
    status = 500
    payload = {"status": "error", "message": "Internal server error"}

    if isinstance(exc, BadRequestError):
        status = 400
        payload["message"] = str(exc) or "Bad request"
    elif isinstance(exc, NotFoundError):
        status = 404
        payload["message"] = str(exc) or "Not found"
    elif isinstance(exc, UnauthorizedError):
        status = 401
        payload["message"] = str(exc) or "Unauthorized"
    elif isinstance(exc, ExternalServiceError):
        status = 502
        payload["message"] = str(exc) or "External service error"
        if getattr(exc, "code", None):
            payload["external_code"] = exc.code
    elif isinstance(exc, VydraError):
        # generic app-level error
        status = 400
        payload["message"] = str(exc) or "Vydra error"
    else:
        # non-Vydra exceptions => 500
        status = 500
        payload["message"] = str(exc) or "Internal server error"

    if include_trace:
        try:
            payload["trace"] = _build_traceback(exc)
        except Exception:
            payload["trace"] = repr(exc)

    return payload, status

def log_exception(exc: BaseException, context: Optional[dict] = None) -> None:
    """
    Log an exception with optional context. Uses module logger.
    """
    try:
        ctx = "" if not context else f" | context={context}"
        tb = _build_traceback(exc)
        _logger.error("Exception: %s%s\n%s", str(exc), ctx, tb)
    except Exception:
        try:
            _logger.error("Exception: %s", str(exc))
        except Exception:
            pass

# -------------------------
# Decorator to wrap functions
# -------------------------
def handle_exceptions(default_status: int = 500, include_trace: Optional[bool] = None):
    """
    Decorator for wrapping callables so they never raise raw exceptions to callers.
    The wrapped function should either:
      - return a Flask-style (payload, status) tuple, or
      - return a payload dict (status 200 assumed), or
      - raise exceptions (these will be caught and converted).

    Usage:

    @handle_exceptions()
    def my_func(...):
        ...
        return {"ok": True}, 200
    """
    def _decorator(fn: Callable):
        def _wrapped(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
                # if result already (payload, status)
                if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
                    return result
                # otherwise wrap into (result, 200)
                return (result, 200)
            except Exception as exc:
                # log and format
                log_exception(exc, context={"fn": getattr(fn, "__name__", str(fn))})
                payload, status = format_error_response(exc, include_trace=include_trace)
                # if a default_status forced, override
                if default_status is not None and status >= 500:
                    status = default_status
                return payload, status
        _wrapped.__name__ = getattr(fn, "__name__", "wrapped")
        return _wrapped
    return _decorator

# -------------------------
# Flask integration helper
# -------------------------
def register_flask_handlers(app) -> None:
    """
    Attach global error handlers to a Flask app.
    Call this once in your app factory after creating the Flask app object.

    Example:
        from error_handler import register_flask_handlers
        register_flask_handlers(app)
    """
    try:
        from flask import jsonify, make_response
    except Exception:
        # Flask not available; nothing to register
        return

    @app.errorhandler(Exception)
    def _handle_all(exc):
        # Always log
        log_exception(exc)
        payload, status = format_error_response(exc)
        return make_response(jsonify(payload), status)

    # Optional specific handlers for common HTTP-related errors
    @app.errorhandler(404)
    def _not_found(e):
        payload, status = format_error_response(NotFoundError("Resource not found"))
        return make_response(jsonify(payload), status)

    @app.errorhandler(400)
    def _bad_request(e):
        payload, status = format_error_response(BadRequestError("Bad request"))
        return make_response(jsonify(payload), status)

# -------------------------
# Minimal self-test when run directly
# -------------------------
if __name__ == "__main__":
    # quick smoke test of formatting and decorator
    print("error_handler self-test (debug=%s)" % (_debug_mode()))
    try:
        @handle_exceptions()
        def bad():
            raise ValueError("x failed")

        payload, status = bad()
        print("Decorated bad() ->", status, payload)
    except Exception as e:
        print("Self-test failed:", e)
