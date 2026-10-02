from pathlib import Path

import pytest

from tubeaudio import cli
from tubeaudio.cli import data_lock, main
from tubeaudio.downloader import DownloadResult
from tubeaudio.store import ChannelQueue

A, B = "aaaaaaaaaaa", "bbbbbbbbbbb"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def fake_network(monkeypatch):
    calls = {"listed": [], "downloaded": []}

    def fake_list(channel, auth=None):
        calls["listed"].append(channel.key)
        return [A, B]

    def fake_download(video_id, audio_dir, stop_event, *, auth=None):
        calls["downloaded"].append(video_id)
        path = audio_dir / f"{video_id}.opus"
        path.write_bytes(b"x")
        return DownloadResult(video_id, path, skipped=False, duration=60)

    monkeypatch.setattr(cli, "list_channel_video_ids", fake_list)
    monkeypatch.setattr(cli, "download_audio", fake_download)
    monkeypatch.setattr(cli, "check_ffmpeg", lambda: None)
    return calls


def test_status_with_no_data(data_dir: Path, capsys):
    assert main(["status", "--data-dir", str(data_dir)]) == 0
    assert "No channels collected yet" in capsys.readouterr().out


def test_collect_then_status(data_dir: Path, fake_network, capsys):
    assert main(["collect", "--channel", "@Demo", "--data-dir", str(data_dir)]) == 0
    assert fake_network["listed"] == ["@demo"]
    assert len(ChannelQueue(data_dir / "channels" / "@demo").pending()) == 2
    assert main(["status", "--data-dir", str(data_dir)]) == 0
    out = capsys.readouterr().out
    assert "@demo" in out and "TOTAL" in out


def test_run_downloads_everything(data_dir: Path, fake_network):
    code = main(["run", "--channel", "@demo", "--data-dir", str(data_dir), "--sleep", "0", "0"])
    assert code == 0
    q = ChannelQueue(data_dir / "channels" / "@demo")
    assert q.pending() == [] and q.done_ids() == {A, B}
    assert list((data_dir / "logs").glob("*.log"))


def test_download_respects_limit(data_dir: Path, fake_network):
    main(["collect", "--channel", "@demo", "--data-dir", str(data_dir)])
    assert (
        main(["download", "--channel", "@demo", "--data-dir", str(data_dir), "--sleep", "0", "0", "--limit", "1"]) == 0
    )
    assert fake_network["downloaded"] == [A]


def test_download_skips_channel_never_collected(data_dir: Path, fake_network, capsys):
    assert main(["download", "--channel", "@new", "--data-dir", str(data_dir), "--sleep", "0", "0"]) == 0
    assert fake_network["downloaded"] == []
    assert "run collect first" in capsys.readouterr().err
    assert not (data_dir / "channels" / "@new").exists()


def test_download_refuses_when_ffmpeg_broken(data_dir: Path, fake_network, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "check_ffmpeg", lambda: "ffmpeg is installed but broken (dyld); fix it with: brew reinstall ffmpeg"
    )
    assert main(["download", "--channel", "@demo", "--data-dir", str(data_dir)]) == 1
    assert "brew reinstall ffmpeg" in capsys.readouterr().err
    assert fake_network["downloaded"] == []


def test_second_process_is_refused(data_dir: Path, fake_network, capsys):
    with data_lock(data_dir):
        assert main(["collect", "--channel", "@demo", "--data-dir", str(data_dir)]) == 1
    assert "another tubeaudio process" in capsys.readouterr().err
    assert fake_network["listed"] == []


def test_missing_channels_file(tmp_path: Path, data_dir: Path, capsys):
    code = main(["collect", "--channels-file", str(tmp_path / "nope.txt"), "--data-dir", str(data_dir)])
    assert code == 1
    assert "nope.txt not found" in capsys.readouterr().err


def test_channels_file_is_used(tmp_path: Path, data_dir: Path, fake_network):
    channels = tmp_path / "channels.txt"
    channels.write_text("# test\n@One\n@two\n", encoding="utf-8")
    assert main(["collect", "--channels-file", str(channels), "--data-dir", str(data_dir)]) == 0
    assert fake_network["listed"] == ["@one", "@two"]


def test_bad_sleep_range(data_dir: Path, fake_network, capsys):
    assert main(["download", "--channel", "@demo", "--data-dir", str(data_dir), "--sleep", "5", "1"]) == 1
    assert "--sleep" in capsys.readouterr().err


def test_unknown_browser(data_dir: Path, fake_network, capsys):
    assert (
        main(["collect", "--channel", "@demo", "--data-dir", str(data_dir), "--cookies-from-browser", "netscape4"]) == 1
    )
    assert "unsupported browser" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == "tubeaudio 0.1.0"
