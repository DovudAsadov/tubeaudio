from pathlib import Path

import pytest

from tubeaudio.channels import Channel, channel_dir, comment_out_channels, load_channels, parse_channel

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


@pytest.mark.parametrize(
    ("text", "tabs"),
    [
        ("@SomeChannel", ("videos",)),
        ("https://www.youtube.com/@SomeChannel", ("videos",)),
        ("https://www.youtube.com/@SomeChannel/videos", ("videos",)),
        ("https://m.youtube.com/@SomeChannel/featured", ("videos",)),
        ("https://www.youtube.com/@SomeChannel/streams", ("streams",)),
        ("youtube.com/@SomeChannel/streams?si=abc", ("streams",)),
        (f"https://www.youtube.com/channel/{CID}/streams", ("streams",)),
    ],
)
def test_parse_channel_tab(text, tabs):
    assert parse_channel(text).tabs == tabs


def test_urls():
    assert Channel("@abc").urls == ("https://www.youtube.com/@abc/videos",)
    assert Channel(CID, ("videos", "streams")).urls == (
        f"https://www.youtube.com/channel/{CID}/videos",
        f"https://www.youtube.com/channel/{CID}/streams",
    )


def test_channel_dir(tmp_path: Path):
    assert channel_dir(tmp_path, Channel("@abc")) == tmp_path / "channels" / "@abc"


def test_load_channels_skips_comments_blanks_and_duplicates(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text(
        f"# my channels\n\n@First\nhttps://www.youtube.com/@first/videos\n   # indented comment\n{CID}\n",
        encoding="utf-8",
    )
    assert [c.key for c in load_channels(path)] == ["@first", CID]


def test_load_channels_merges_tabs_of_one_channel(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text(
        "https://www.youtube.com/@One/streams\n@two\nhttps://www.youtube.com/@one/videos\n"
        "https://www.youtube.com/@three/streams\n",
        encoding="utf-8",
    )
    assert load_channels(path) == [
        Channel("@one", ("videos", "streams")),
        Channel("@two", ("videos",)),
        Channel("@three", ("streams",)),
    ]


def test_load_channels_reports_bad_line(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text("@ok\nnot a channel\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"channels\.txt:2"):
        load_channels(path)


def test_load_channels_allows_inline_comments(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text(f"@one  # main news channel\n{CID}\t# lectures\n", encoding="utf-8")
    assert [c.key for c in load_channels(path)] == ["@one", CID]


def test_comment_out_channels(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text(
        "# my list\n\nhttps://www.youtube.com/@One/videos\n@two  # keep me\n# @three\n",
        encoding="utf-8",
    )
    done = comment_out_channels(path, {"@one": "finished 2026-10-02, 12 videos", "@three": "x"})
    assert done == ["@one"]  # "@three" is already commented out
    assert path.read_text(encoding="utf-8") == (
        "# my list\n\n"
        "# https://www.youtube.com/@One/videos  # finished 2026-10-02, 12 videos\n"
        "@two  # keep me\n# @three\n"
    )
    assert [c.key for c in load_channels(path)] == ["@two"]
    assert not list(tmp_path.glob("*.tmp"))


def test_comment_out_channels_comments_every_tab_line(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text("https://www.youtube.com/@one/videos\nhttps://www.youtube.com/@one/streams\n", encoding="utf-8")
    assert comment_out_channels(path, {"@one": "finished"}) == ["@one"]
    assert load_channels(path) == []


def test_uncommenting_a_finished_channel_brings_it_back(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text("@one\n", encoding="utf-8")
    comment_out_channels(path, {"@one": "finished 2026-10-02, 3 videos"})
    path.write_text(path.read_text(encoding="utf-8").removeprefix("# "), encoding="utf-8")
    assert [c.key for c in load_channels(path)] == ["@one"]


def test_comment_out_channels_nothing_to_do_leaves_file_untouched(tmp_path: Path):
    path = tmp_path / "channels.txt"
    path.write_text("@one\n", encoding="utf-8")
    before = path.stat().st_mtime_ns
    assert comment_out_channels(path, {"@other": "finished"}) == []
    assert path.stat().st_mtime_ns == before
