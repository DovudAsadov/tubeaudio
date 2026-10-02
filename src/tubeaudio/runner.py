"""Work through channel download queues with a pool of worker threads."""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .channels import Channel
from .downloader import Cancelled, DownloadFailed, DownloadResult
from .store import ChannelQueue, video_id_from_url
from .ytdlp import ErrorKind

log = logging.getLogger("tubeaudio")

DownloadFn = Callable[[str, Path, threading.Event], DownloadResult]


class Outcome(Enum):
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    TRANSIENT = "transient"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    FATAL = "fatal"


@dataclass(frozen=True)
class RunConfig:
    workers: int = 1
    limit: int | None = None  # max new downloads this run
    sleep_min: float = 2.0
    sleep_max: float = 6.0
    retry_delays: tuple[float, ...] = (10.0, 30.0, 90.0)
    # Stop when this many videos in a row exhaust their retries: something is wrong
    # with the setup (outdated yt-dlp, cookies, network), not with the videos.
    max_consecutive_failures: int = 5


@dataclass
class RunSummary:
    downloaded: int = 0
    already_present: int = 0
    failed: int = 0
    left_pending: int = 0
    blocked: bool = False
    fatal: bool = False
    stalled: bool = False
    cancelled: bool = False

    @property
    def exit_code(self) -> int:
        if self.blocked:
            return 2
        if self.fatal or self.stalled:
            return 1
        if self.cancelled:
            return 130
        return 0


@dataclass(frozen=True)
class _Item:
    channel: Channel
    queue: ChannelQueue
    url: str
    index: int
    total: int


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_size(path: Path) -> str:
    try:
        return f"{path.stat().st_size / 1_000_000:.1f} MB"
    except OSError:
        return "? MB"


def _short(message: str, limit: int = 160) -> str:
    text = " ".join(message.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Runner:
    def __init__(self, config: RunConfig, download: DownloadFn, stop_event: threading.Event | None = None) -> None:
        self.config = config
        self._download = download
        self.stop_event = stop_event or threading.Event()
        self._consecutive_give_ups = 0  # only touched by the main thread (_record)
        self._last_error = ""

    def run(self, queues: list[tuple[Channel, ChannelQueue]]) -> RunSummary:
        summary = RunSummary()
        items = self._items(queues)
        pool = ThreadPoolExecutor(max_workers=self.config.workers, thread_name_prefix="download")
        in_flight: set[Future[Outcome]] = set()
        wait_for_workers = True
        try:
            while True:
                while self._can_submit(summary, in_flight):
                    item = next(items, None)
                    if item is None:
                        break
                    in_flight.add(pool.submit(self._process, item))
                if not in_flight:
                    break
                finished, in_flight = wait(in_flight, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in finished:
                    self._record(summary, future.result())
        except KeyboardInterrupt:
            summary.cancelled = True
            self.stop_event.set()
            log.warning("Stopping: aborting in-flight downloads (press Ctrl+C again to quit immediately)…")
            try:
                for future in in_flight:
                    self._record(summary, future.result())
            except KeyboardInterrupt:
                wait_for_workers = False
                raise
        except BaseException:
            self.stop_event.set()
            raise
        finally:
            pool.shutdown(wait=wait_for_workers, cancel_futures=True)
        return summary

    def _items(self, queues: list[tuple[Channel, ChannelQueue]]) -> Iterator[_Item]:
        for channel, queue in queues:
            pending = queue.pending()
            for index, url in enumerate(pending, start=1):
                yield _Item(channel, queue, url, index, len(pending))

    def _can_submit(self, summary: RunSummary, in_flight: set[Future[Outcome]]) -> bool:
        if self.stop_event.is_set() or len(in_flight) >= self.config.workers:
            return False
        limit = self.config.limit
        return limit is None or summary.downloaded + len(in_flight) < limit

    def _record(self, summary: RunSummary, outcome: Outcome) -> None:
        if outcome in (Outcome.DONE, Outcome.SKIPPED):
            self._consecutive_give_ups = 0
        if outcome is Outcome.DONE:
            summary.downloaded += 1
        elif outcome is Outcome.SKIPPED:
            summary.already_present += 1
        elif outcome is Outcome.FAILED:
            summary.failed += 1
        elif outcome is Outcome.TRANSIENT:
            summary.left_pending += 1
            self._consecutive_give_ups += 1
            if self._consecutive_give_ups >= self.config.max_consecutive_failures and not summary.stalled:
                summary.stalled = True
                self.stop_event.set()
                log.error(
                    "Stopping: %d videos in a row failed to download (last error: %s). "
                    "Likely causes: outdated yt-dlp (fix: uv lock --upgrade-package yt-dlp && uv sync), "
                    "bad cookies, no network, or a full disk. The queue is intact; fix the cause and run again.",
                    self._consecutive_give_ups,
                    _short(self._last_error),
                )
        elif outcome is Outcome.BLOCKED:
            summary.blocked = True
        elif outcome is Outcome.FATAL:
            summary.fatal = True

    def _pause(self) -> None:
        delay = random.uniform(self.config.sleep_min, self.config.sleep_max)
        if delay > 0:
            self.stop_event.wait(delay)

    def _process(self, item: _Item) -> Outcome:
        tag = f"[{item.channel.key} {item.index}/{item.total}]"
        video_id = video_id_from_url(item.url)
        if video_id is None:  # pending.txt edited while running
            item.queue.mark_failed(item.url, item.url, "unrecognized video URL or ID")
            return Outcome.FAILED
        delays = self.config.retry_delays
        attempt = 0
        while True:
            if self.stop_event.is_set():
                return Outcome.CANCELLED
            attempt += 1
            try:
                result = self._download(video_id, item.queue.audio_dir, self.stop_event)
            except Cancelled:
                return Outcome.CANCELLED
            except DownloadFailed as exc:
                if exc.kind in (ErrorKind.BLOCKED, ErrorKind.FATAL):
                    log.error("%s ! %s %s", tag, video_id, _short(exc.message))
                    self.stop_event.set()
                    return Outcome.BLOCKED if exc.kind is ErrorKind.BLOCKED else Outcome.FATAL
                if exc.kind is ErrorKind.PERMANENT:
                    item.queue.mark_failed(video_id, item.url, exc.message, attempts=attempt)
                    log.warning("%s ✗ %s %s", tag, video_id, _short(exc.message))
                    self._pause()
                    return Outcome.FAILED
                if attempt > len(delays):
                    self._last_error = exc.message
                    log.warning(
                        "%s … %s still failing after %d attempts, left in pending: %s",
                        tag,
                        video_id,
                        attempt,
                        _short(exc.message),
                    )
                    self._pause()
                    return Outcome.TRANSIENT
                delay = delays[attempt - 1]
                log.info(
                    "%s ↻ %s attempt %d failed, retrying in %.0fs: %s",
                    tag,
                    video_id,
                    attempt,
                    delay,
                    _short(exc.message),
                )
                if self.stop_event.wait(delay):
                    return Outcome.CANCELLED
                continue
            item.queue.mark_done(video_id)
            if result.skipped:
                log.info("%s = %s already on disk", tag, video_id)
                return Outcome.SKIPPED
            log.info(
                "%s ✓ %s %s (format %s, %s) %s %s",
                tag,
                video_id,
                result.path.suffix.lstrip("."),
                result.format_id or "?",
                f"{result.abr:.0f}k" if result.abr else "?k",
                _fmt_size(result.path),
                _fmt_duration(result.duration),
            )
            self._pause()
            return Outcome.DONE
