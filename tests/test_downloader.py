import subprocess
import threading
from pathlib import Path

import pytest
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError

from tubeaudio import downloader
from tubeaudio.downloader import (
    Cancelled,
    DownloadFailed,
    build_download_opts,
    check_ffmpeg,
    download_audio,
    find_existing,
)
from tubeaudio.ytdlp import Auth, ErrorKind

VID = "jNQXAC9IVRw"


def make_done(audio_dir: Path, ext: str = "opus") -> Path:
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / f"{VID}.info.json").write_text("{}", encoding="utf-8")
    path = audio_dir / f"{VID}.{ext}"
    path.write_bytes(b"audio")
    return path


def test_find_existing_complete(tmp_path: Path):
    path = make_done(tmp_path)
    assert find_existing(tmp_path, VID) == path
    assert find_existing(tmp_path, "otherid0000") is None


@pytest.mark.parametrize(
    "leftover", [f"{VID}.webm", f"{VID}.opus.part", f"{VID}.webm.part", f"{VID}.temp.opus", f"{VID}.webm.ytdl"]
)
def test_find_existing_rejects_unfinished(tmp_path: Path, leftover):
    make_done(tmp_path)
    (tmp_path / leftover).write_bytes(b"x")
    assert find_existing(tmp_path, VID) is None


def test_find_existing_needs_info_json_and_nonempty_audio(tmp_path: Path):
    (tmp_path / f"{VID}.opus").write_bytes(b"audio")
    assert find_existing(tmp_path, VID) is None  # no info.json
    (tmp_path / f"{VID}.info.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"{VID}.opus").write_bytes(b"")
    assert find_existing(tmp_path, VID) is None  # empty audio


def test_build_download_opts(tmp_path: Path):
    hook = lambda d: None  # noqa: E731
    opts = build_download_opts(tmp_path, Auth(), hook)
    # Full-quality DASH/https audio only: no HLS fallback, no muxed-video fallback.
    assert opts["format"] == "bestaudio[protocol^=http]"
    assert opts["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]
    assert opts["paths"] == {"home": str(tmp_path)}
    assert opts["outtmpl"] == "%(id)s.%(ext)s"
    assert opts["writeinfojson"] is True and opts["continuedl"] is True and opts["noplaylist"] is True
    assert opts["progress_hooks"] == [hook]


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; `behaviour(fake, url)` decides what happens."""

    behaviour = None
    instances: list["FakeYDL"] = []

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        return type(self).behaviour(self, url)


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.instances = []
    monkeypatch.setattr(downloader, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_download_audio_success(tmp_path: Path, fake_ydl):
    def behaviour(ydl, url):
        assert url == f"https://www.youtube.com/watch?v={VID}"
        out = tmp_path / f"{VID}.opus"
        out.write_bytes(b"audio")
        return {"duration": 19, "requested_downloads": [{"filepath": str(out), "format_id": "251", "abr": 124.5}]}

    fake_ydl.behaviour = behaviour
    result = download_audio(VID, tmp_path, threading.Event())
    assert result.path == tmp_path / f"{VID}.opus"
    assert result.skipped is False
    assert result.duration == 19
    assert (result.format_id, result.abr) == ("251", 124.5)


def test_download_audio_unexpected_exception_is_transient(tmp_path: Path, fake_ydl):
    def behaviour(ydl, url):
        raise TypeError("'NoneType' object is not subscriptable")  # a yt-dlp extractor bug

    fake_ydl.behaviour = behaviour
    with pytest.raises(DownloadFailed) as info:
        download_audio(VID, tmp_path, threading.Event())
    assert info.value.kind is ErrorKind.TRANSIENT
    assert "TypeError" in info.value.message


def test_download_audio_cookie_load_error_is_fatal(tmp_path: Path, fake_ydl):
    def behaviour(ydl, url):
        raise CookieLoadError("failed to load cookies")

    fake_ydl.behaviour = behaviour
    with pytest.raises(DownloadFailed) as info:
        download_audio(VID, tmp_path, threading.Event())
    assert info.value.kind is ErrorKind.FATAL


def test_download_audio_never_modifies_user_cookie_file(tmp_path: Path, fake_ydl):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\nORIGINAL\n", encoding="utf-8")
    used = []

    def behaviour(ydl, url):
        private = Path(ydl.opts["cookiefile"])
        used.append(private)
        assert private != cookies
        assert private.read_text(encoding="utf-8") == cookies.read_text(encoding="utf-8")
        private.write_text("CORRUPTED by yt-dlp saving on close", encoding="utf-8")
        raise DownloadError("ERROR: [youtube] x: Private video")

    fake_ydl.behaviour = behaviour
    with pytest.raises(DownloadFailed):
        download_audio(VID, tmp_path / "audio", threading.Event(), auth=Auth(cookies_file=cookies))
    assert cookies.read_text(encoding="utf-8") == "# Netscape HTTP Cookie File\nORIGINAL\n"
    assert not used[0].exists()  # private copy cleaned up


def test_download_audio_skips_existing(tmp_path: Path, fake_ydl):
    path = make_done(tmp_path)
    result = download_audio(VID, tmp_path, threading.Event())
    assert result.skipped is True and result.path == path
    assert fake_ydl.instances == []


def test_download_audio_classifies_errors(tmp_path: Path, fake_ydl):
    def behaviour(ydl, url):
        raise DownloadError("ERROR: [youtube] x: Private video. Sign in if you've been granted access")

    fake_ydl.behaviour = behaviour
    with pytest.raises(DownloadFailed) as info:
        download_audio(VID, tmp_path, threading.Event())
    assert info.value.kind is ErrorKind.PERMANENT
    assert "Private video" in info.value.message


def test_download_audio_cancels_via_progress_hook(tmp_path: Path, fake_ydl):
    stop = threading.Event()

    def behaviour(ydl, url):
        stop.set()  # user pressed Ctrl+C mid-download
        ydl.opts["progress_hooks"][0]({"status": "downloading"})
        raise AssertionError("hook should have raised")

    fake_ydl.behaviour = behaviour
    with pytest.raises(Cancelled):
        download_audio(VID, tmp_path, stop)


def test_check_ffmpeg_ok(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        downloader.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"ffmpeg version 8", b"")
    )
    assert check_ffmpeg() is None


def test_check_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda name: None)
    assert "ffmpeg not found" in check_ffmpeg()


def test_check_ffmpeg_broken(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        downloader.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, -6, b"", b"dyld[1]: Library not loaded: libx265.216.dylib\n"),
    )
    problem = check_ffmpeg()
    assert "broken" in problem and "Library not loaded" in problem and "brew reinstall ffmpeg" in problem
