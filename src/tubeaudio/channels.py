"""Parse channels.txt entries into channels with stable folder names."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

_CHANNEL_ID = r"UC[A-Za-z0-9_-]{22}"
_CHANNEL_ID_RE = re.compile(rf"^{_CHANNEL_ID}$")
_HANDLE_RE = re.compile(r"^@[^/?#\s]+$")
_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/"
    rf"(?:(?P<handle>@[^/?#\s]+)|channel/(?P<cid>{_CHANNEL_ID}))"
    r"(?:/(?P<tab>[^/?#\s]*))?(?:[/?#].*)?$"
)
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")

# Channel pages whose videos can be collected, in collection order.
TABS = ("videos", "streams")


@dataclass(frozen=True)
class Channel:
    """A YouTube channel; `key` is its folder name: '@handle' (lowercase) or 'UC…' id.

    `tabs` are the channel pages to collect videos from: 'videos' and/or 'streams'.
    """

    key: str
    tabs: tuple[str, ...] = ("videos",)

    @property
    def urls(self) -> tuple[str, ...]:
        path = self.key if self.key.startswith("@") else f"channel/{self.key}"
        return tuple(f"https://www.youtube.com/{path}/{tab}" for tab in self.tabs)


def _handle_key(handle: str) -> str:
    # Handles are case-insensitive on YouTube; lowercasing keeps one folder per channel.
    return unquote(handle).lower()


def parse_channel(text: str) -> Channel:
    s = text.strip()
    if _HANDLE_RE.match(s):
        return Channel(_handle_key(s))
    if _CHANNEL_ID_RE.match(s):
        return Channel(s)
    match = _URL_RE.match(s)
    if match:
        # Only a /streams URL selects the Streams tab; any other page means the Videos tab.
        tabs = ("streams",) if match["tab"] == "streams" else ("videos",)
        if match["handle"]:
            return Channel(_handle_key(match["handle"]), tabs)
        return Channel(match["cid"], tabs)
    raise ValueError(f"not a YouTube channel @handle, channel ID, or channel URL: {s!r}")


def _entry(raw: str) -> str:
    """The channel text of a channels.txt line, without comments ('' for comment/blank lines)."""
    line = raw.strip()
    if line.startswith("#"):
        return ""
    return _INLINE_COMMENT_RE.sub("", line)


def merge_channels(channels: Iterable[Channel]) -> list[Channel]:
    """One Channel per key, in first-seen order, with the tabs of all its entries."""
    tabs: dict[str, set[str]] = {}
    for channel in channels:
        tabs.setdefault(channel.key, set()).update(channel.tabs)
    return [Channel(key, tuple(t for t in TABS if t in found)) for key, found in tabs.items()]


def load_channels(path: Path) -> list[Channel]:
    channels: list[Channel] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = _entry(raw)
        if not line:
            continue
        try:
            channel = parse_channel(line)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from None
        channels.append(channel)
    return merge_channels(channels)


def channel_dir(data_dir: Path, channel: Channel) -> Path:
    return data_dir / "channels" / channel.key


def comment_out_channels(path: Path, notes: dict[str, str]) -> list[str]:
    """Comment out the lines of channels whose key is in `notes`, appending that note.

    Returns the keys commented out, in file order. The file is rewritten atomically,
    and left untouched when nothing matches.
    """
    out: list[str] = []
    changed: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines(keepends=True):
        entry = _entry(raw)
        try:
            key = parse_channel(entry).key if entry else None
        except ValueError:
            key = None
        if key in notes:
            body = raw.rstrip("\r\n")
            out.append(f"# {body.strip()}  # {notes[key]}{raw[len(body) :]}")
            if key not in changed:
                changed.append(key)
        else:
            out.append(raw)
    if changed:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(out)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    return changed
