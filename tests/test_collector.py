from pathlib import Path

import pytest
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError

from tubeaudio import collector
from tubeaudio.channels import Channel
from tubeaudio.collector import CollectFailed, collect_all, collect_channel, list_channel_video_ids
from tubeaudio.store import ChannelQueue, video_url
from tubeaudio.ytdlp import ErrorKind

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"
CH1, CH2 = Channel("@one"), Channel("@two")


class FakeYDL:
    result = None
    error = None
    opts = None

    def __init__(self, opts):
        FakeYDL.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        assert url == CH1.videos_url and download is False
        if FakeYDL.error:
            raise FakeYDL.error
        return FakeYDL.result


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.result, FakeYDL.error, FakeYDL.opts = None, None, None
    monkeypatch.setattr(collector, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_list_channel_video_ids_flat_and_deduped(fake_ydl):
    fake_ydl.result = {"entries": [{"id": A}, None, {"id": B}, {"id": A}, {"title": "no id"}]}
    assert list_channel_video_ids(CH1) == [A, B]
    assert fake_ydl.opts["extract_flat"] == "in_playlist"


def test_list_channel_video_ids_skips_upcoming_and_live(fake_ydl):
    fake_ydl.result = {
        "entries": [
            {"id": A, "live_status": "is_upcoming"},  # scheduled premiere: not downloadable yet
            {"id": B, "live_status": "is_live"},  # would record a truncated live stream
            {"id": C, "live_status": "was_live"},
            {"id": "ddddddddddd", "live_status": None},
        ]
    }
    assert list_channel_video_ids(CH1) == [C, "ddddddddddd"]


def test_list_channel_video_ids_unexpected_exception(fake_ydl):
    fake_ydl.error = TypeError("yt-dlp extractor bug")
    with pytest.raises(CollectFailed) as info:
        list_channel_video_ids(CH1)
    assert info.value.kind is ErrorKind.TRANSIENT
    assert "TypeError" in info.value.message


def test_list_channel_video_ids_cookie_load_error_is_fatal(fake_ydl):
    fake_ydl.error = CookieLoadError("failed to load cookies")
    with pytest.raises(CollectFailed) as info:
        list_channel_video_ids(CH1)
    assert info.value.kind is ErrorKind.FATAL


def test_list_channel_video_ids_empty_channel(fake_ydl):
    fake_ydl.result = {"entries": []}
    assert list_channel_video_ids(CH1) == []


def test_list_channel_video_ids_wraps_errors(fake_ydl):
    fake_ydl.error = DownloadError("ERROR: [youtube:tab] UCx: This channel does not have a videos tab")
    with pytest.raises(CollectFailed) as info:
        list_channel_video_ids(CH1)
    assert info.value.kind is ErrorKind.TRANSIENT
    assert "videos tab" in info.value.message


def test_collect_channel_adds_only_new(tmp_path: Path):
    q = ChannelQueue(tmp_path / "channels" / "@one")
    q.add_new([A])
    q.mark_done(A)
    result = collect_channel(CH1, tmp_path, lambda ch: [A, B, C])
    assert (result.listed, result.added) == (3, 2)
    assert ChannelQueue(tmp_path / "channels" / "@one").pending() == [video_url(B), video_url(C)]
    assert collect_channel(CH1, tmp_path, lambda ch: [A, B, C]).added == 0


def test_collect_all_continues_after_failed_channel(tmp_path: Path):
    def list_ids(ch):
        if ch == CH1:
            raise CollectFailed(ErrorKind.TRANSIENT, "HTTP Error 404: Not Found")
        return [A]

    summary = collect_all([CH1, CH2], tmp_path, list_ids)
    assert summary.exit_code == 1
    assert summary.listed == [CH2]  # only channels whose listing succeeded
    assert not (tmp_path / "channels" / "@one").exists()  # no empty folder for a bad channel
    assert ChannelQueue(tmp_path / "channels" / "@two").pending() == [video_url(A)]


def test_collect_all_stops_when_blocked(tmp_path: Path):
    seen = []

    def list_ids(ch):
        seen.append(ch)
        raise CollectFailed(ErrorKind.BLOCKED, "Sign in to confirm you're not a bot")

    summary = collect_all([CH1, CH2], tmp_path, list_ids)
    assert (summary.exit_code, summary.listed) == (2, [])
    assert seen == [CH1]


def test_collect_all_ok(tmp_path: Path):
    summary = collect_all([CH1, CH2], tmp_path, lambda ch: [A, B])
    assert (summary.exit_code, summary.listed) == (0, [CH1, CH2])
