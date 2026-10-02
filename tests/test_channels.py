from pathlib import Path

import pytest

from tubeaudio.channels import Channel, channel_dir, load_channels, parse_channel

CID = "UCkH_kZuTvTDw5ojt-aqTolQ"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("@SomeChannel", "@somechannel"),
        ("  @some.channel-1  ", "@some.channel-1"),
        ("https://www.youtube.com/@SomeChannel", "@somechannel"),
        ("https://youtube.com/@SomeChannel/videos", "@somechannel"),
        ("youtube.com/@SomeChannel?si=abc", "@somechannel"),
        ("https://m.youtube.com/@SomeChannel/featured", "@somechannel"),
        ("https://www.youtube.com/@%D0%9A%D0%B0%D0%BD%D0%B0%D0%BB", "@канал"),
        (f"https://www.youtube.com/channel/{CID}", CID),
        (f"https://www.youtube.com/channel/{CID}/videos", CID),
        (CID, CID),
    ],
)
def test_parse_channel(text, key):
    assert parse_channel(text).key == key


@pytest.mark.parametrize(
    "text",
    [
        "",
        "SomeChannel",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://example.com/@someone",
        "https://www.youtube.com/c/LegacyName",
    ],
)
def test_parse_channel_rejects(text):
    with pytest.raises(ValueError):
        parse_channel(text)


def test_videos_url():
    assert Channel("@abc").videos_url == "https://www.youtube.com/@abc/videos"
    assert Channel(CID).videos_url == f"https://www.youtube.com/channel/{CID}/videos"


def test_channel_dir(tmp_path: Path):
    assert channel_dir(tmp_path, Channel("@abc")) == tmp_path / "channels" / "@abc"


def test_load_channels_skips_comments_blanks_and_duplicates(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text(
        f"# my channels\n\n@First\nhttps://www.youtube.com/@first/videos\n   # indented comment\n{CID}\n",
        encoding="utf-8",
    )
    assert [c.key for c in load_channels(path)] == ["@first", CID]


def test_load_channels_reports_bad_line(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text("@ok\nnot a channel\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"channels\.txt:2"):
        load_channels(path)
