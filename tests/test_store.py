import threading
from pathlib import Path

from tubeaudio.store import ChannelQueue, read_counts, video_id_from_url, video_url

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_video_id_from_url():
    assert video_id_from_url(f"https://www.youtube.com/watch?v={A}") == A
    assert video_id_from_url(f"https://www.youtube.com/watch?feature=x&v={A}&t=3") == A
    assert video_id_from_url(f"https://youtu.be/{A}?si=z") == A
    assert video_id_from_url(f"https://www.youtube.com/shorts/{A}") == A
    assert video_id_from_url(f"  {A}  ") == A
    assert video_id_from_url("garbage") is None
    assert video_id_from_url("https://www.youtube.com/watch?v=short") is None


def test_new_queue_is_empty(tmp_path: Path):
    q = ChannelQueue(tmp_path / "ch")
    assert q.pending() == []
    assert q.counts().pending == q.counts().done == q.counts().failed == 0
    assert q.audio_dir.is_dir()


def test_add_new_appends_in_order_and_dedupes(tmp_path: Path):
    q = ChannelQueue(tmp_path)
    assert q.add_new([A, B, A]) == 2
    assert q.pending() == [video_url(A), video_url(B)]
    assert q.add_new([B, C]) == 1
    assert q.pending() == [video_url(A), video_url(B), video_url(C)]


def test_add_new_skips_done_and_failed(tmp_path: Path):
    q = ChannelQueue(tmp_path)
    q.add_new([A, B])
    q.mark_done(A)
    q.mark_failed(B, video_url(B), "Private video")
    assert q.add_new([A, B, C]) == 1
    assert q.pending() == [video_url(C)]


def test_mark_done_moves_from_pending_to_done(tmp_path: Path):
    q = ChannelQueue(tmp_path)
    q.add_new([A, B])
    q.mark_done(A)
    assert q.pending() == [video_url(B)]
    assert lines(tmp_path / "done.txt") == [A]
    q.mark_done(A)  # idempotent
    assert lines(tmp_path / "done.txt") == [A]


def test_mark_failed_writes_sanitized_tsv_row(tmp_path: Path):
    q = ChannelQueue(tmp_path)
    q.add_new([A])
    q.mark_failed(A, video_url(A), "ERROR: Private video\tline2\nmore", attempts=2)
    assert q.pending() == []
    rows = lines(tmp_path / "failed.tsv")
    assert rows[0] == "video_id\turl\tattempts\terror\ttimestamp"
    vid, url, attempts, error, timestamp = rows[1].split("\t")
    assert (vid, url, attempts) == (A, video_url(A), "2")
    assert error == "ERROR: Private video line2 more"
    assert timestamp
    assert q.failed()[A].attempts == 2


def test_retry_failed_moves_back_to_pending(tmp_path: Path):
    q = ChannelQueue(tmp_path)
    q.add_new([A, B])
    q.mark_failed(A, video_url(A), "boom")
    assert q.retry_failed() == 1
    assert q.pending() == [video_url(B), video_url(A)]
    assert q.failed() == {}
    assert q.retry_failed() == 0


def test_reconcile_on_load_repairs_hand_edits_and_crashes(tmp_path: Path):
    (tmp_path / "pending.txt").write_text(
        "\n".join(
            [
                video_url(A),  # already in done.txt: crash between append and rewrite
                f"https://youtu.be/{B}",  # short link -> normalized
                "",
                video_url(B),  # duplicate
                "not a url",  # junk -> failed.tsv
                video_url(C),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "done.txt").write_text(A + "\n", encoding="utf-8")
    q = ChannelQueue(tmp_path)
    assert q.pending() == [video_url(B), video_url(C)]
    assert "not a url" in q.failed()
    assert not list(tmp_path.glob("*.tmp"))


def test_append_after_truncated_last_line(tmp_path: Path):
    (tmp_path / "done.txt").write_text(A, encoding="utf-8")  # no trailing newline
    q = ChannelQueue(tmp_path)
    q.add_new([B])
    q.mark_done(B)
    assert lines(tmp_path / "done.txt") == [A, B]


def test_concurrent_mark_done_is_consistent(tmp_path: Path):
    ids = [f"{i:011d}" for i in range(200)]
    q = ChannelQueue(tmp_path)
    q.add_new(ids)
    threads = [threading.Thread(target=q.mark_done, args=(vid,)) for vid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert q.pending() == []
    assert sorted(lines(tmp_path / "done.txt")) == sorted(ids)


def test_read_counts_does_not_modify_files(tmp_path: Path):
    pending = tmp_path / "pending.txt"
    pending.write_text(f"{video_url(A)}\n{video_url(A)}\n", encoding="utf-8")
    before = pending.read_text(encoding="utf-8")
    assert read_counts(tmp_path).pending == 2  # raw count, nothing repaired
    assert pending.read_text(encoding="utf-8") == before
