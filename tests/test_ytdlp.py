import logging
from pathlib import Path

import pytest

from tubeaudio.ytdlp import Auth, ErrorKind, base_opts, classify_error, detect_js_runtimes


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        (
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
            ErrorKind.PERMANENT,
        ),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", ErrorKind.PERMANENT),
        (
            "ERROR: [youtube] abc: Join this channel to get access to members-only content like this video",
            ErrorKind.PERMANENT,
        ),
        (
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
            ErrorKind.PERMANENT,
        ),
        # Not aired yet: keep it pending so a later run gets it (not failed.tsv forever).
        ("ERROR: [youtube] abc: This live event will begin in 3 days.", ErrorKind.TRANSIENT),
        ("ERROR: [youtube] abc: Premieres in 5 hours", ErrorKind.TRANSIENT),
        (
            "ERROR: [youtube] abc: Video unavailable. "
            "This video contains content from SME, who has blocked it on copyright grounds",
            ErrorKind.PERMANENT,
        ),
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser", ErrorKind.BLOCKED),
        ("ERROR: [youtube] abc: Sign in to confirm you're not a bot.", ErrorKind.BLOCKED),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", ErrorKind.BLOCKED),
        ("ERROR: [youtube] abc: Video unavailable. This content isn’t available, try again later.", ErrorKind.BLOCKED),
        ("ERROR: unable to download video data: <urlopen error [Errno 60] Operation timed out>", ErrorKind.TRANSIENT),
        ("ERROR: unable to download video data: HTTP Error 503: Service Unavailable", ErrorKind.TRANSIENT),
        ("ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path", ErrorKind.FATAL),
        ("ERROR: unable to write data: [Errno 28] No space left on device", ErrorKind.FATAL),
        # Disk-full first surfaces as an info.json write failure with no errno text.
        ("ERROR: Cannot write video metadata to JSON file data/channels/@x/audio/abc.info.json", ErrorKind.FATAL),
        # Words in file paths (here "copyright" in a channel handle) must not win over FATAL.
        (
            "ERROR: Cannot write video metadata to JSON file data/channels/@nocopyrightsounds/audio/a.info.json",
            ErrorKind.FATAL,
        ),
        ("PermissionError: [Errno 13] Permission denied: 'data/channels/@x/audio/abc.opus'", ErrorKind.FATAL),
        ("OSError: [Errno 1] Operation not permitted: 'data/channels/@x/audio'", ErrorKind.FATAL),
    ],
)
def test_classify_error(message, kind):
    assert classify_error(message) is kind


def test_quality_warnings_reach_the_console_once(caplog):
    logger = base_opts(Auth())["logger"]
    challenge = (
        "[youtube] abc: n challenge solving failed: Some formats may be missing. "
        "Ensure you have a supported JavaScript runtime"
    )
    with caplog.at_level(logging.DEBUG, logger="tubeaudio"):
        logger.warning(challenge)
        logger.warning(challenge)
        logger.warning("[youtube] abc: Falling back to generic n function search")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [challenge]


def test_detect_js_runtimes_prefers_deno_then_node(monkeypatch):
    available = {"node": "/usr/bin/node", "bun": "/usr/bin/bun"}
    monkeypatch.setattr("tubeaudio.ytdlp.shutil.which", available.get)
    assert detect_js_runtimes() == {"node": {}}
    monkeypatch.setattr("tubeaudio.ytdlp.shutil.which", lambda name: None)
    assert detect_js_runtimes() == {}


def test_base_opts_with_auth(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("tubeaudio.ytdlp.shutil.which", lambda name: "/x" if name == "node" else None)
    opts = base_opts(Auth(cookies_file=tmp_path / "c.txt", cookies_from_browser="chrome"))
    assert opts["js_runtimes"] == {"node": {}}
    assert opts["cookiefile"] == str(tmp_path / "c.txt")
    assert opts["cookiesfrombrowser"] == ("chrome",)
    assert opts["quiet"] is True and opts["noprogress"] is True
    assert hasattr(opts["logger"], "debug") and hasattr(opts["logger"], "warning")


def test_base_opts_without_auth_or_runtime(monkeypatch):
    monkeypatch.setattr("tubeaudio.ytdlp.shutil.which", lambda name: None)
    opts = base_opts(Auth())
    assert "js_runtimes" not in opts
    assert "cookiefile" not in opts and "cookiesfrombrowser" not in opts
