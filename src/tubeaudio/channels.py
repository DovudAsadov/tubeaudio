"""Parse channels.txt entries into channels with stable folder names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

_CHANNEL_ID = r"UC[A-Za-z0-9_-]{22}"
_CHANNEL_ID_RE = re.compile(rf"^{_CHANNEL_ID}$")
_HANDLE_RE = re.compile(r"^@[^/?#\s]+$")
_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/"
    rf"(?:(?P<handle>@[^/?#\s]+)|channel/(?P<cid>{_CHANNEL_ID}))"
    r"(?:[/?#].*)?$"
)


@dataclass(frozen=True)
class Channel:
    """A YouTube channel; `key` is its folder name: '@handle' (lowercase) or 'UC…' id."""

    key: str

    @property
    def videos_url(self) -> str:
        path = self.key if self.key.startswith("@") else f"channel/{self.key}"
        return f"https://www.youtube.com/{path}/videos"


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
        if match["handle"]:
            return Channel(_handle_key(match["handle"]))
        return Channel(match["cid"])
    raise ValueError(f"not a YouTube channel @handle, channel ID, or channel URL: {s!r}")


def load_channels(path: Path) -> list[Channel]:
    channels: list[Channel] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            channel = parse_channel(line)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from None
        if channel.key not in seen:
            seen.add(channel.key)
            channels.append(channel)
    return channels


def channel_dir(data_dir: Path, channel: Channel) -> Path:
    return data_dir / "channels" / channel.key
