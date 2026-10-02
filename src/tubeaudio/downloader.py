"""Download one video's best audio stream (copied, never re-encoded) plus its info.json."""

from __future__ import annotations

import glob
import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError, PostProcessingError

from .store import video_url
from .ytdlp import Auth, ErrorKind, base_opts, classify_error, private_cookies

_log = logging.getLogger("tubeaudio.downloader")

AUDIO_EXTS = ("opus", "m4a", "ogg", "mp3", "aac", "flac", "wav")
# Files that mean a previous attempt didn't finish (download or audio extraction).
_LEFTOVER_SUFFIXES = (".part", ".ytdl", ".webm")


class DownloadFailed(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class Cancelled(Exception):
    """The download was aborted because a stop was requested."""


@dataclass(frozen=True)
class DownloadResult:
    video_id: str
    path: Path
    skipped: bool  # True when the audio was already complete on disk
    duration: float | None = None
    format_id: str | None = None  # YouTube format chosen, e.g. "251" (Opus)
    abr: float | None = None  # audio bitrate in kbit/s


def find_existing(audio_dir: Path, video_id: str) -> Path | None:
    """Return the finished audio file for `video_id`, or None if it isn't complete."""
    if not (audio_dir / f"{video_id}.info.json").is_file():
        return None
    siblings = glob.glob(glob.escape(str(audio_dir / video_id)) + ".*")
    if any(s.endswith(_LEFTOVER_SUFFIXES) or ".temp." in s for s in siblings):
        return None
    for ext in AUDIO_EXTS:
        path = audio_dir / f"{video_id}.{ext}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def build_download_opts(audio_dir: Path, auth: Auth, progress_hook: Callable[[dict], None]) -> dict:
    opts = base_opts(auth)
    opts.update(
        {
            # Best audio-only stream over plain https/DASH. HLS (m3u8) and muxed video
            # formats are what remains when YouTube's challenges fail; refusing them
            # keeps the video pending instead of silently saving lower-quality audio.
            "format": "bestaudio[protocol^=http]",
            "paths": {"home": str(audio_dir)},
            "outtmpl": "%(id)s.%(ext)s",
            "writeinfojson": True,
            # preferredcodec "best" = stream copy: .webm(opus) -> .opus, aac stays .m4a
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}],
            "continuedl": True,
            "noplaylist": True,
            "progress_hooks": [progress_hook],
        }
    )
    return opts


def download_audio(
    video_id: str, audio_dir: Path, stop_event: threading.Event, *, auth: Auth | None = None
) -> DownloadResult:
    existing = find_existing(audio_dir, video_id)
    if existing is not None:
        return DownloadResult(video_id, existing, skipped=True)

    def stop_hook(_status: dict) -> None:
        if stop_event.is_set():
            raise DownloadCancelled("stop requested")

    audio_dir.mkdir(parents=True, exist_ok=True)
    try:
        with private_cookies(auth or Auth()) as session_auth:
            with YoutubeDL(build_download_opts(audio_dir, session_auth, stop_hook)) as ydl:
                info = ydl.extract_info(video_url(video_id), download=True)
    except DownloadCancelled as exc:
        raise Cancelled(str(exc)) from exc
    except CookieLoadError as exc:
        raise DownloadFailed(ErrorKind.FATAL, f"could not load cookies: {exc}") from exc
    except (DownloadError, ExtractorError, PostProcessingError) as exc:
        message = str(exc)
        raise DownloadFailed(classify_error(message), message) from exc
    except Exception as exc:  # a yt-dlp bug on one odd video must not stop the run
        _log.debug("unexpected error downloading %s", video_id, exc_info=True)
        raise DownloadFailed(ErrorKind.TRANSIENT, f"{type(exc).__name__}: {exc}") from exc

    downloads = (info or {}).get("requested_downloads") or []
    if not downloads or not downloads[0].get("filepath"):
        raise DownloadFailed(ErrorKind.TRANSIENT, "yt-dlp finished but reported no audio file")
    chosen = downloads[0]
    return DownloadResult(
        video_id,
        Path(chosen["filepath"]),
        skipped=False,
        duration=info.get("duration"),
        format_id=chosen.get("format_id"),
        abr=chosen.get("abr"),
    )


def check_ffmpeg() -> str | None:
    """Return None if ffmpeg and ffprobe both run, else a message saying how to fix it."""
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if path is None:
            return f"{tool} not found on PATH; install it (macOS: brew install ffmpeg)"
        try:
            proc = subprocess.run([path, "-version"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{tool} could not be started: {exc}"
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout).decode(errors="replace").strip().splitlines()
            detail = output[0] if output else f"exit code {proc.returncode}"
            return f"{tool} is installed but broken ({detail}); fix it with: brew reinstall ffmpeg"
    return None
