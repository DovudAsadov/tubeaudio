"""Per-channel resumable download queue stored as plain text files.

pending.txt  one video URL per line, in download order; a line is removed as
             soon as that video is downloaded (or fails permanently)
done.txt     one video URL per line, appended after each successful download
             (older files with bare video IDs are still understood)
failed.tsv   permanent failures: video_id, url, attempts, error, timestamp
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PENDING_FILE = "pending.txt"
DONE_FILE = "done.txt"
FAILED_FILE = "failed.tsv"
FAILED_HEADER = "video_id\turl\tattempts\terror\ttimestamp"

_ID = r"[A-Za-z0-9_-]{11}"
_ID_RE = re.compile(rf"^{_ID}$")
_URL_ID_RE = re.compile(rf"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)({_ID})(?![A-Za-z0-9_-])")
_MAX_ERROR_LEN = 500


@dataclass(frozen=True)
class FailedEntry:
    video_id: str
    url: str
    attempts: int
    error: str
    timestamp: str


@dataclass(frozen=True)
class QueueCounts:
    pending: int
    done: int
    failed: int


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def video_id_from_url(text: str) -> str | None:
    s = text.strip()
    if _ID_RE.match(s):
        return s
    match = _URL_ID_RE.search(s)
    return match.group(1) if match else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean(text: str) -> str:
    return " ".join(text.split())[:_MAX_ERROR_LEN]


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [s for line in path.read_text(encoding="utf-8").splitlines() if (s := line.strip())]


def _write_lines_atomic(path: Path, lines: Iterable[str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _append_line(path: Path, line: str) -> None:
    with open(path, "a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":  # previous append was cut off mid-line
                fh.write(b"\n")
        fh.write(line.encode("utf-8") + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def _read_ids(path: Path) -> set[str]:
    return {vid for line in _read_lines(path) if (vid := video_id_from_url(line))}


def _read_failed(path: Path) -> dict[str, FailedEntry]:
    entries: dict[str, FailedEntry] = {}
    for line in _read_lines(path):
        if line == FAILED_HEADER:
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        vid, url, attempts, error, timestamp = parts
        entries[vid] = FailedEntry(vid, url, int(attempts) if attempts.isdigit() else 0, error, timestamp)
    return entries


def read_counts(root: Path) -> QueueCounts:
    """Count queue entries without repairing anything (safe while a download runs)."""
    return QueueCounts(
        pending=len(_read_lines(root / PENDING_FILE)),
        done=len(_read_ids(root / DONE_FILE)),
        failed=len(_read_failed(root / FAILED_FILE)),
    )


class ChannelQueue:
    """The download queue of one channel folder. All methods are thread-safe."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.audio_dir = root / "audio"
        self._pending_path = root / PENDING_FILE
        self._done_path = root / DONE_FILE
        self._failed_path = root / FAILED_FILE
        self._lock = threading.RLock()
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._reconcile()

    def pending(self) -> list[str]:
        with self._lock:
            return _read_lines(self._pending_path)

    def done_ids(self) -> set[str]:
        with self._lock:
            return _read_ids(self._done_path)

    def failed(self) -> dict[str, FailedEntry]:
        with self._lock:
            return _read_failed(self._failed_path)

    def counts(self) -> QueueCounts:
        with self._lock:
            return read_counts(self.root)

    def add_new(self, video_ids: Iterable[str]) -> int:
        """Append IDs not already pending, done or failed. Returns how many were added."""
        with self._lock:
            pending = self.pending()
            known = {video_id_from_url(u) for u in pending} | self.done_ids() | set(self.failed())
            new: list[str] = []
            for vid in video_ids:
                if vid not in known:
                    known.add(vid)
                    new.append(video_url(vid))
            if new:
                _write_lines_atomic(self._pending_path, pending + new)
            return len(new)

    def mark_done(self, video_id: str) -> None:
        # Record in done.txt first: if we crash before the pending rewrite,
        # _reconcile() drops the stale pending line on the next start.
        with self._lock:
            if video_id not in self.done_ids():
                _append_line(self._done_path, video_url(video_id))
            self._remove_pending({video_id})

    def mark_failed(self, video_id: str, url: str, error: str, attempts: int = 1) -> None:
        with self._lock:
            failed = self.failed()
            failed[video_id] = FailedEntry(video_id, url, attempts, _clean(error), _now())
            self._write_failed(failed)
            self._remove_pending({video_id})

    def retry_failed(self) -> int:
        """Move every failed entry back to the end of pending. Returns how many moved."""
        with self._lock:
            failed = self.failed()
            if not failed:
                return 0
            pending = self.pending()
            in_pending = {video_id_from_url(u) for u in pending}
            pending += [e.url for e in failed.values() if e.video_id not in in_pending]
            _write_lines_atomic(self._pending_path, pending)
            self._write_failed({})
            return len(failed)

    def _write_failed(self, failed: dict[str, FailedEntry]) -> None:
        rows = ["\t".join([e.video_id, e.url, str(e.attempts), e.error, e.timestamp]) for e in failed.values()]
        _write_lines_atomic(self._failed_path, [FAILED_HEADER, *rows])

    def _remove_pending(self, video_ids: set[str]) -> None:
        pending = self.pending()
        kept = [u for u in pending if video_id_from_url(u) not in video_ids]
        if len(kept) != len(pending):
            _write_lines_atomic(self._pending_path, kept)

    def _reconcile(self) -> None:
        """Repair state after crashes or hand edits.

        Normalizes pending URLs, drops duplicates and entries already done or
        failed, and moves unrecognizable lines to failed.tsv.
        """
        with self._lock:
            pending = self.pending()
            done = self.done_ids()
            failed = self.failed()
            kept: list[str] = []
            seen: set[str] = set()
            junk = False
            for line in pending:
                vid = video_id_from_url(line)
                if vid is None:
                    key = _clean(line)
                    failed[key] = FailedEntry(key, key, 0, "unrecognized video URL or ID", _now())
                    junk = True
                    continue
                if vid in done or vid in failed or vid in seen:
                    continue
                seen.add(vid)
                kept.append(video_url(vid))
            if junk:
                self._write_failed(failed)
            if kept != pending:
                _write_lines_atomic(self._pending_path, kept)
