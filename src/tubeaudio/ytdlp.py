"""yt-dlp options shared by listing and downloading, and error classification."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

_log = logging.getLogger("tubeaudio.ytdlp")

JS_RUNTIME_PREFERENCE = ("deno", "node", "bun")

BLOCKED_ADVICE = (
    "YouTube is rate-limiting or bot-checking this machine; the queue is intact. "
    "Wait about an hour and run again, or add --cookies-from-browser chrome (or --cookies FILE)."
)


@dataclass(frozen=True)
class Auth:
    cookies_file: Path | None = None
    cookies_from_browser: str | None = None


class ErrorKind(Enum):
    PERMANENT = "permanent"  # this video will never download: record in failed.tsv
    TRANSIENT = "transient"  # retry with backoff, then leave in pending
    BLOCKED = "blocked"  # YouTube is throttling us: stop the whole run
    FATAL = "fatal"  # local environment is broken (disk, ffmpeg): stop the whole run


_FATAL_MARKERS = (
    "no space left on device",
    "disk quota exceeded",
    "read-only file system",
    "cannot write",  # e.g. "Cannot write video metadata to JSON file" (often a full disk)
    "permission denied",
    "operation not permitted",
    "ffmpeg not found",
    "ffprobe and ffmpeg not found",
)
_BLOCKED_MARKERS = (
    "http error 429",
    "too many requests",
    "not a bot",
    "rate-limited",
    "try again later",
)
_PERMANENT_MARKERS = (
    "private video",
    "video unavailable",
    "has been removed",
    "members-only",
    "join this channel",
    "copyright",
    "account associated with this video has been terminated",
    # Not listed on purpose: "live event will begin" / "premieres in". A video that
    # hasn't aired yet stays pending (TRANSIENT) so a later run downloads it.
    "confirm your age",
    "age-restricted",
    "inappropriate for some users",
    "not made this video available in your country",
)


def classify_error(message: str) -> ErrorKind:
    text = message.lower()
    for kind, markers in (
        (ErrorKind.FATAL, _FATAL_MARKERS),
        (ErrorKind.BLOCKED, _BLOCKED_MARKERS),
        (ErrorKind.PERMANENT, _PERMANENT_MARKERS),
    ):
        if any(marker in text for marker in markers):
            return kind
    return ErrorKind.TRANSIENT


_QUALITY_WARNINGS = (
    "challenge solving failed",
    "signature solving failed",
    "formats may be missing",
    "no supported javascript runtime",
)
_VIDEO_PREFIX_RE = re.compile(r"^\[[^\]]+\] [^:]+: ")


class _YtdlpLogger:
    """Send yt-dlp output to the log file (DEBUG); our own code reports outcomes.

    Warnings that mean the best audio formats may be missing are also shown on
    the console, once per distinct message.
    """

    _seen: set[str] = set()
    _seen_lock = threading.Lock()

    def debug(self, msg: str) -> None:
        _log.debug(msg)

    info = error = debug

    def warning(self, msg: str) -> None:
        if any(marker in msg.lower() for marker in _QUALITY_WARNINGS):
            key = _VIDEO_PREFIX_RE.sub("", msg)
            with self._seen_lock:
                first = key not in self._seen
                self._seen.add(key)
            if first:
                _log.warning(msg)
                return
        _log.debug(msg)


def detect_js_runtimes() -> dict[str, dict]:
    """yt-dlp needs a JS runtime to solve YouTube player challenges."""
    for name in JS_RUNTIME_PREFERENCE:
        if shutil.which(name):
            return {name: {}}
    return {}


def base_opts(auth: Auth) -> dict:
    opts: dict = {
        "quiet": True,
        "noprogress": True,
        "logger": _YtdlpLogger(),
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }
    runtimes = detect_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    if auth.cookies_file:
        opts["cookiefile"] = str(auth.cookies_file)
    if auth.cookies_from_browser:
        opts["cookiesfrombrowser"] = (auth.cookies_from_browser,)
    return opts


@contextmanager
def private_cookies(auth: Auth) -> Iterator[Auth]:
    """Give yt-dlp a throwaway copy of the cookies file.

    yt-dlp rewrites its cookie file whenever a YoutubeDL instance closes; with one
    instance per video and several workers, that would race on (and could corrupt)
    the user's own file.
    """
    if auth.cookies_file is None:
        yield auth
        return
    fd, tmp = tempfile.mkstemp(prefix="tubeaudio-cookies-", suffix=".txt")
    try:
        with os.fdopen(fd, "wb") as dst, open(auth.cookies_file, "rb") as src:
            shutil.copyfileobj(src, dst)
        yield replace(auth, cookies_file=Path(tmp))
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
