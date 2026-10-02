"""List every video on a channel's Videos tab and queue the new ones."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError, ExtractorError

from .channels import Channel, channel_dir
from .store import ChannelQueue
from .ytdlp import BLOCKED_ADVICE, Auth, ErrorKind, base_opts, classify_error, private_cookies

log = logging.getLogger("tubeaudio")

ListIds = Callable[[Channel], list[str]]

# Scheduled premieres can't be downloaded yet, and a running live stream would be
# recorded truncated. Leaving them out lets a later collect add them once aired.
_NOT_AIRED = ("is_upcoming", "is_live")


class CollectFailed(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class CollectResult:
    listed: int
    added: int


def list_channel_video_ids(channel: Channel, auth: Auth | None = None) -> list[str]:
    """IDs on the channel's Videos tab, newest first. One flat listing, no per-video requests."""
    try:
        with private_cookies(auth or Auth()) as session_auth:
            opts = base_opts(session_auth)
            opts.update({"extract_flat": "in_playlist", "skip_download": True})
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(channel.videos_url, download=False)
    except CookieLoadError as exc:
        raise CollectFailed(ErrorKind.FATAL, f"could not load cookies: {exc}") from exc
    except (DownloadError, ExtractorError) as exc:
        raise CollectFailed(classify_error(str(exc)), str(exc)) from exc
    except Exception as exc:  # a yt-dlp bug must not stop the other channels
        log.debug("unexpected error listing %s", channel.key, exc_info=True)
        raise CollectFailed(ErrorKind.TRANSIENT, f"{type(exc).__name__}: {exc}") from exc
    ids: list[str] = []
    seen: set[str] = set()
    not_aired = 0
    for entry in (info or {}).get("entries") or []:
        entry = entry or {}
        vid = entry.get("id")
        if entry.get("live_status") in _NOT_AIRED:
            not_aired += 1
            continue
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(vid)
    if not_aired:
        log.info("%s: skipping %d upcoming/live video(s); a later collect will add them", channel.key, not_aired)
    return ids


def collect_channel(channel: Channel, data_dir: Path, list_ids: ListIds) -> CollectResult:
    ids = list_ids(channel)  # list first, so a bad channel never gets a folder
    queue = ChannelQueue(channel_dir(data_dir, channel))
    return CollectResult(listed=len(ids), added=queue.add_new(ids))


def collect_all(channels: list[Channel], data_dir: Path, list_ids: ListIds) -> int:
    """Collect every channel. Returns 0 if all listed, 1 if some failed, 2 if blocked."""
    exit_code = 0
    for channel in channels:
        log.info("%s: listing videos…", channel.key)
        try:
            result = collect_channel(channel, data_dir, list_ids)
        except CollectFailed as exc:
            log.error("%s: listing failed: %s", channel.key, exc.message)
            if exc.kind in (ErrorKind.BLOCKED, ErrorKind.FATAL):
                if exc.kind is ErrorKind.BLOCKED:
                    log.error(BLOCKED_ADVICE)
                return 2 if exc.kind is ErrorKind.BLOCKED else 1
            exit_code = 1
            continue
        log.info("%s: %d listed, %d new", channel.key, result.listed, result.added)
    return exit_code
