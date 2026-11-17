"""Debug output for the library, also when it is used from another program.

Two loggers:

    FreshService        retries, rate-limit pauses, fallbacks, maintenance actions
    FreshService.http   every request: method, path, query, status, time, attempt,
                        rate-limit headroom and Freshservice's x-request-id; request
                        bodies, and the response body of every failed request

Both are standard `logging` loggers, so an application that configures logging gets them
like any other (`logging.getLogger("FreshService").setLevel(logging.DEBUG)`). Without
logging set up, call enable_debug(), set FRESH_DEBUG=true / FRESH_DEBUG_FILE=path, or pass
FreshServiceClient(debug=True).

The API key is never logged (it travels in the session's basic auth, not in params or
bodies). Bodies are cut at MAX_LOGGED_CHARS: ticket text can be long and personal, so
debug files deserve the same care as the data itself.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, TextIO

log = logging.getLogger("FreshService")
http_log = logging.getLogger("FreshService.http")

MAX_LOGGED_CHARS = 2000
DEBUG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
_HANDLER_MARK = "_freshservice_debug"


def clip(value: Any, limit: int = MAX_LOGGED_CHARS) -> str:
    """A loggable, length-limited rendering of a body or response."""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    text = " ".join(text.split())
    if not text:
        return "(empty)"
    return text if len(text) <= limit else f"{text[:limit]}... ({len(text) - limit} more characters)"


def enable_debug(
    *,
    file: str | Path | None = None,
    stream: TextIO | None = None,
    http: bool = True,
    level: int = logging.DEBUG,
) -> None:
    """Show the library's debug output.

    file: also append it to this file. stream: write it there (default stderr), but only
    if nothing else would show it - if the application (or the CLI) already has logging
    handlers, those are used and no second copy is printed. http=False keeps the
    per-request lines out. Safe to call more than once.
    """
    log.setLevel(level)
    http_log.setLevel(level if http else max(level, logging.INFO))
    formatter = logging.Formatter(DEBUG_FORMAT)
    marked = [handler for handler in log.handlers if getattr(handler, _HANDLER_MARK, None)]
    if file is not None:
        target = str(Path(file).resolve())
        if not any(getattr(h, _HANDLER_MARK, None) == target for h in marked):
            handler: logging.Handler = logging.FileHandler(target, encoding="utf-8")
            handler.setFormatter(formatter)
            setattr(handler, _HANDLER_MARK, target)
            log.addHandler(handler)
    shown_elsewhere = bool(logging.getLogger().handlers) or any(
        getattr(h, _HANDLER_MARK, None) == "stream" for h in marked
    )
    has_stream = any(getattr(h, _HANDLER_MARK, None) == "stream" for h in marked)
    if (stream is not None or not shown_elsewhere) and not has_stream:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, "stream")
        log.addHandler(handler)


def disable_debug() -> None:
    """Undo enable_debug(): remove its handlers and reset the levels."""
    for handler in list(log.handlers):
        if getattr(handler, _HANDLER_MARK, None):
            log.removeHandler(handler)
            handler.close()
    log.setLevel(logging.NOTSET)
    http_log.setLevel(logging.NOTSET)
