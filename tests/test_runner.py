import errno
import logging
import threading
import time
from pathlib import Path

from tubeaudio.channels import Channel
from tubeaudio.downloader import Cancelled, DownloadFailed, DownloadResult
from tubeaudio.runner import RunConfig, Runner
from tubeaudio.store import ChannelQueue, video_url
from tubeaudio.ytdlp import ErrorKind

FAST = dict(sleep_min=0, sleep_max=0, retry_delays=(0, 0, 0), min_free_bytes=0)
GB = 1_000_000_000


def ids(n: int, prefix: str = "v") -> list[str]:
    return [f"{prefix}{i:010d}" for i in range(n)]


class FakeDownload:
    """script maps video_id -> list of steps: "ok" | "skip" | "cancel" | "interrupt" | ErrorKind."""

    def __init__(self, script=None, on_call=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[str] = []
        self.on_call = on_call
        self._lock = threading.Lock()

    def __call__(self, video_id: str, audio_dir: Path, stop_event: threading.Event) -> DownloadResult:
        with self._lock:
            self.calls.append(video_id)
            steps = self.script.get(video_id)
            step = steps.pop(0) if steps else "ok"
        if self.on_call:
            self.on_call(video_id, stop_event)
        if step == "ok":
            path = audio_dir / f"{video_id}.opus"
            path.write_bytes(b"x" * 1000)
            return DownloadResult(video_id, path, skipped=False, duration=65, format_id="251", abr=124.5)
        if step == "skip":
            return DownloadResult(video_id, audio_dir / f"{video_id}.opus", skipped=True)
        if step == "cancel":
            raise Cancelled("stop requested")
        if step == "interrupt":
            raise KeyboardInterrupt
        raise DownloadFailed(step, f"{step.value} problem")


def make_queue(tmp_path: Path, key: str, video_ids: list[str]) -> tuple[Channel, ChannelQueue]:
    q = ChannelQueue(tmp_path / key)
    q.add_new(video_ids)
    return Channel(key), q


def test_success_moves_everything_to_done(tmp_path: Path):
    ch, q = make_queue(tmp_path, "@a", ids(3))
    summary = Runner(RunConfig(**FAST), FakeDownload()).run([(ch, q)])
    assert summary.downloaded == 3 and summary.exit_code == 0
    assert q.pending() == [] and q.done_ids() == set(ids(3))


def test_permanent_failure_goes_to_failed(tmp_path: Path):
    a, b = ids(2)
    ch, q = make_queue(tmp_path, "@a", [a, b])
    summary = Runner(RunConfig(**FAST), FakeDownload({a: [ErrorKind.PERMANENT]})).run([(ch, q)])
    assert summary.failed == 1 and summary.downloaded == 1
    assert q.pending() == [] and set(q.failed()) == {a} and q.done_ids() == {b}


def test_transient_failure_retries_then_succeeds(tmp_path: Path):
    (a,) = ids(1)
    ch, q = make_queue(tmp_path, "@a", [a])
    fake = FakeDownload({a: [ErrorKind.TRANSIENT, ErrorKind.TRANSIENT, "ok"]})
    summary = Runner(RunConfig(**FAST), fake).run([(ch, q)])
    assert fake.calls == [a, a, a] and summary.downloaded == 1 and q.pending() == []


def test_transient_failure_gives_up_and_stays_pending(tmp_path: Path):
    a, b = ids(2)
    ch, q = make_queue(tmp_path, "@a", [a, b])
    fake = FakeDownload({a: [ErrorKind.TRANSIENT] * 4})
    summary = Runner(RunConfig(**FAST), fake).run([(ch, q)])
    assert fake.calls.count(a) == 4  # 1 try + 3 retries
    assert summary.left_pending == 1 and summary.downloaded == 1
    assert q.pending() == [video_url(a)] and q.failed() == {}


def test_blocked_stops_the_whole_run(tmp_path: Path):
    a, b = ids(2)
    ch, q = make_queue(tmp_path, "@a", [a, b])
    fake = FakeDownload({a: [ErrorKind.BLOCKED]})
    summary = Runner(RunConfig(**FAST), fake).run([(ch, q)])
    assert summary.blocked and summary.exit_code == 2
    assert fake.calls == [a]
    assert q.pending() == [video_url(a), video_url(b)]


def test_fatal_stops_the_whole_run(tmp_path: Path):
    a, b = ids(2)
    ch, q = make_queue(tmp_path, "@a", [a, b])
    summary = Runner(RunConfig(**FAST), FakeDownload({a: [ErrorKind.FATAL]})).run([(ch, q)])
    assert summary.fatal and summary.exit_code == 1
    assert q.pending() == [video_url(a), video_url(b)]


def test_limit_counts_only_new_downloads(tmp_path: Path):
    vids = ids(5)
    ch, q = make_queue(tmp_path, "@a", vids)
    fake = FakeDownload({vids[0]: ["skip"]})
    summary = Runner(RunConfig(limit=2, **FAST), fake).run([(ch, q)])
    assert summary.already_present == 1 and summary.downloaded == 2
    assert len(q.pending()) == 2


def test_limit_is_exact_with_many_workers(tmp_path: Path):
    ch, q = make_queue(tmp_path, "@a", ids(10))
    fake = FakeDownload()
    summary = Runner(RunConfig(workers=4, limit=3, **FAST), fake).run([(ch, q)])
    assert summary.downloaded == 3 and len(fake.calls) == 3 and len(q.pending()) == 7


def test_parallel_workers_keep_queue_consistent(tmp_path: Path):
    vids = ids(40)
    ch, q = make_queue(tmp_path, "@a", vids)
    summary = Runner(RunConfig(workers=4, **FAST), FakeDownload()).run([(ch, q)])
    assert summary.downloaded == 40 and q.pending() == [] and q.done_ids() == set(vids)


def test_channels_processed_in_order(tmp_path: Path):
    one = make_queue(tmp_path, "@one", ids(2, "a"))
    two = make_queue(tmp_path, "@two", ids(2, "b"))
    fake = FakeDownload()
    Runner(RunConfig(**FAST), fake).run([one, two])
    assert fake.calls == ids(2, "a") + ids(2, "b")


def test_ctrl_c_leaves_items_pending(tmp_path: Path):
    a, b = ids(2)
    ch, q = make_queue(tmp_path, "@a", [a, b])
    fake = FakeDownload({a: ["interrupt"]})
    summary = Runner(RunConfig(**FAST), fake).run([(ch, q)])
    assert summary.cancelled and summary.exit_code == 130
    assert fake.calls == [a]
    assert q.pending() == [video_url(a), video_url(b)] and q.failed() == {}


def test_consecutive_give_ups_stop_the_run(tmp_path: Path):
    vids = ids(6)
    ch, q = make_queue(tmp_path, "@a", vids)
    fake = FakeDownload({v: [ErrorKind.TRANSIENT] * 4 for v in vids})
    summary = Runner(RunConfig(max_consecutive_failures=3, **FAST), fake).run([(ch, q)])
    assert summary.stalled and summary.exit_code == 1
    assert len(fake.calls) == 3 * 4  # stopped after 3 videos in a row gave up
    assert len(q.pending()) == 6 and q.failed() == {}


def test_success_resets_consecutive_give_ups(tmp_path: Path):
    a, b, c, d, e = ids(5)
    ch, q = make_queue(tmp_path, "@a", [a, b, c, d, e])
    fake = FakeDownload({v: [ErrorKind.TRANSIENT] * 4 for v in (a, c, e)})
    summary = Runner(RunConfig(max_consecutive_failures=2, **FAST), fake).run([(ch, q)])
    assert not summary.stalled and summary.exit_code == 0
    assert summary.downloaded == 2 and summary.left_pending == 3


def test_done_line_shows_format_and_bitrate(tmp_path: Path, caplog):
    ch, q = make_queue(tmp_path, "@a", ids(1))
    with caplog.at_level(logging.INFO, logger="tubeaudio"):
        Runner(RunConfig(**FAST), FakeDownload()).run([(ch, q)])
    done_lines = [r.getMessage() for r in caplog.records if "✓" in r.getMessage()]
    assert len(done_lines) == 1 and "251" in done_lines[0] and "124k" in done_lines[0]


def test_stop_during_retry_backoff_is_prompt(tmp_path: Path):
    (a,) = ids(1)
    ch, q = make_queue(tmp_path, "@a", [a])
    runner = Runner(RunConfig(sleep_min=0, sleep_max=0, retry_delays=(60,)), None)

    def on_call(video_id, stop_event):
        threading.Timer(0.2, runner.stop_event.set).start()

    runner._download = FakeDownload({a: [ErrorKind.TRANSIENT]}, on_call=on_call)
    started = time.monotonic()
    runner.run([(ch, q)])
    assert time.monotonic() - started < 5
    assert q.pending() == [video_url(a)]


def test_low_disk_space_stops_before_downloading(tmp_path: Path, caplog):
    ch, q = make_queue(tmp_path, "@a", ids(2))
    fake = FakeDownload()
    runner = Runner(RunConfig(**dict(FAST, min_free_bytes=2 * GB)), fake, free_space=lambda path: 1 * GB)
    with caplog.at_level(logging.ERROR, logger="tubeaudio"):
        summary = runner.run([(ch, q)])
    assert summary.low_space and summary.exit_code == 3
    assert fake.calls == [] and len(q.pending()) == 2
    assert "1.0 GB free" in caplog.text


def test_disk_filling_mid_download_keeps_video_pending(tmp_path: Path):
    a, b, c = ids(3)
    ch, q = make_queue(tmp_path, "@a", [a, b, c])
    free = {"bytes": 50 * GB}

    def on_call(video_id, stop_event):
        if video_id == b:
            free["bytes"] = 0  # the disk filled up while b was downloading

    fake = FakeDownload({b: [ErrorKind.FATAL]}, on_call=on_call)
    runner = Runner(RunConfig(**dict(FAST, min_free_bytes=2 * GB)), fake, free_space=lambda path: free["bytes"])
    summary = runner.run([(ch, q)])
    assert summary.low_space and summary.exit_code == 3
    assert fake.calls == [a, b]
    assert q.done_ids() == {a}
    assert q.pending() == [video_url(b), video_url(c)] and q.failed() == {}


def test_disk_full_while_recording_done_keeps_video_pending(tmp_path: Path, monkeypatch):
    (a,) = ids(1)
    ch, q = make_queue(tmp_path, "@a", [a])

    def disk_full(video_id):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(q, "mark_done", disk_full)
    summary = Runner(RunConfig(**FAST), FakeDownload(), free_space=lambda path: 50 * GB).run([(ch, q)])
    assert summary.low_space and summary.exit_code == 3
    assert q.pending() == [video_url(a)]


def test_min_free_zero_disables_the_disk_check(tmp_path: Path):
    ch, q = make_queue(tmp_path, "@a", ids(1))
    summary = Runner(RunConfig(**FAST), FakeDownload(), free_space=lambda path: 0).run([(ch, q)])
    assert summary.downloaded == 1 and not summary.low_space
