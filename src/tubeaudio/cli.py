"""tubeaudio: collect | download | run | status."""

from __future__ import annotations

import argparse
import fcntl
import functools
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from yt_dlp.cookies import SUPPORTED_BROWSERS

from .channels import Channel, channel_dir, load_channels, parse_channel
from .collector import collect_all, list_channel_video_ids
from .downloader import check_ffmpeg, download_audio
from .runner import RunConfig, Runner
from .store import ChannelQueue, read_counts
from .ytdlp import BLOCKED_ADVICE, Auth, detect_js_runtimes

log = logging.getLogger("tubeaudio")


class UsageError(Exception):
    """A problem the user must fix; shown without a traceback, exit code 1."""


# ---------------------------------------------------------------- setup


def _setup_logging(data_dir: Path, verbose: bool, to_file: bool) -> None:
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)
    if to_file:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"{datetime.now():%Y%m%d-%H%M%S}.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
        log.addHandler(file_handler)


@contextmanager
def data_lock(data_dir: Path) -> Iterator[None]:
    """Allow only one writing process per data directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fh = open(data_dir / ".lock", "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UsageError(
                f"another tubeaudio process is already working in {data_dir}/; wait for it to finish or stop it first"
            ) from None
        yield
    finally:
        fh.close()


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _channels(args: argparse.Namespace) -> list[Channel]:
    if args.channel:
        try:
            return list(dict.fromkeys(parse_channel(c) for c in args.channel))
        except ValueError as exc:
            raise UsageError(str(exc)) from None
    path: Path = args.channels_file
    if not path.exists():
        raise UsageError(
            f"{path} not found; create it with one channel per line (@handle or channel URL), or pass --channel"
        )
    try:
        channels = load_channels(path)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    if not channels:
        raise UsageError(f"{path} lists no channels")
    return channels


def _auth(args: argparse.Namespace) -> Auth:
    if args.cookies and not args.cookies.is_file():
        raise UsageError(f"cookies file not found: {args.cookies}")
    browser = args.cookies_from_browser
    if browser and browser.lower() not in SUPPORTED_BROWSERS:
        raise UsageError(f"unsupported browser {browser!r}; choose one of: {', '.join(sorted(SUPPORTED_BROWSERS))}")
    return Auth(cookies_file=args.cookies, cookies_from_browser=browser.lower() if browser else None)


def _run_config(args: argparse.Namespace) -> RunConfig:
    sleep_min, sleep_max = args.sleep
    if sleep_min < 0 or sleep_max < sleep_min:
        raise UsageError("--sleep MIN MAX needs 0 <= MIN <= MAX")
    return RunConfig(workers=args.workers, limit=args.limit, sleep_min=sleep_min, sleep_max=sleep_max)


def _preflight() -> None:
    problem = check_ffmpeg()
    if problem:
        raise UsageError(problem)
    if not detect_js_runtimes():
        log.warning(
            "warning: no JavaScript runtime (deno, node or bun) found; "
            "YouTube downloads may fail. Fix: brew install deno"
        )


# ---------------------------------------------------------------- work


def _collect(channels: list[Channel], data_dir: Path, auth: Auth) -> int:
    return collect_all(channels, data_dir, functools.partial(list_channel_video_ids, auth=auth))


def _download(channels: list[Channel], data_dir: Path, auth: Auth, config: RunConfig, retry_failed: bool) -> int:
    queues: list[tuple[Channel, ChannelQueue]] = []
    for channel in channels:
        folder = channel_dir(data_dir, channel)
        if not folder.exists():
            log.warning("%s: no queue yet, run collect first; skipping", channel.key)
            continue
        queue = ChannelQueue(folder)
        if retry_failed and (moved := queue.retry_failed()):
            log.info("%s: %d failed video(s) moved back to pending", channel.key, moved)
        queues.append((channel, queue))

    total = sum(len(q.pending()) for _, q in queues)
    log.info("%d video(s) pending across %d channel(s); %d worker(s)", total, len(queues), config.workers)
    started = time.monotonic()
    summary = Runner(config, functools.partial(download_audio, auth=auth)).run(queues)
    still_pending = sum(len(q.pending()) for _, q in queues)
    minutes = (time.monotonic() - started) / 60
    log.info(
        "Done in %.1f min: %d downloaded, %d already on disk, %d failed permanently, %d still pending",
        minutes,
        summary.downloaded,
        summary.already_present,
        summary.failed,
        still_pending,
    )
    if summary.blocked:
        log.error(BLOCKED_ADVICE)
    return summary.exit_code


# ---------------------------------------------------------------- commands


def cmd_collect(args: argparse.Namespace) -> int:
    channels, auth = _channels(args), _auth(args)
    with data_lock(args.data_dir):
        return _collect(channels, args.data_dir, auth)


def cmd_download(args: argparse.Namespace) -> int:
    channels, auth, config = _channels(args), _auth(args), _run_config(args)
    _preflight()
    with data_lock(args.data_dir):
        return _download(channels, args.data_dir, auth, config, args.retry_failed)


def cmd_run(args: argparse.Namespace) -> int:
    channels, auth, config = _channels(args), _auth(args), _run_config(args)
    _preflight()
    with data_lock(args.data_dir):
        collect_code = _collect(channels, args.data_dir, auth)
        if collect_code == 2:
            return 2
        return _download(channels, args.data_dir, auth, config, args.retry_failed) or collect_code


def cmd_status(args: argparse.Namespace) -> int:
    root = args.data_dir / "channels"
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not folders:
        print("No channels collected yet. Run: tubeaudio collect")
        return 0
    rows = [(p.name, read_counts(p)) for p in folders]
    width = max(len("channel"), *(len(name) for name, _ in rows))
    print(f"{'channel':<{width}}  {'pending':>8}  {'done':>8}  {'failed':>8}")
    for name, c in rows:
        print(f"{name:<{width}}  {c.pending:>8}  {c.done:>8}  {c.failed:>8}")
    totals = [sum(getattr(c, f) for _, c in rows) for f in ("pending", "done", "failed")]
    print(f"{'TOTAL':<{width}}  {totals[0]:>8}  {totals[1]:>8}  {totals[2]:>8}")
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path, default=Path("data"), help="output folder (default: data)")
    common.add_argument("-v", "--verbose", action="store_true", help="show yt-dlp debug output on the console")

    source = argparse.ArgumentParser(add_help=False)
    source.add_argument("--channels-file", type=Path, default=Path("channels.txt"), help="default: channels.txt")
    source.add_argument(
        "--channel",
        action="append",
        metavar="CHANNEL",
        help="only this channel (@handle, channel ID or URL); repeatable; channels.txt not needed",
    )

    auth = argparse.ArgumentParser(add_help=False)
    auth.add_argument("--cookies", type=Path, metavar="FILE", help="Netscape-format cookies.txt")
    auth.add_argument("--cookies-from-browser", metavar="BROWSER", help="e.g. chrome, firefox, safari")

    download = argparse.ArgumentParser(add_help=False)
    download.add_argument("--workers", type=_positive_int, default=1, help="parallel downloads (default: 1)")
    download.add_argument("--limit", type=_positive_int, help="stop after N new downloads")
    download.add_argument(
        "--sleep",
        type=float,
        nargs=2,
        metavar=("MIN", "MAX"),
        default=(2.0, 6.0),
        help="random pause between downloads in seconds (default: 2 6)",
    )
    download.add_argument("--retry-failed", action="store_true", help="move failed.tsv entries back to pending first")

    parser = argparse.ArgumentParser(
        prog="tubeaudio",
        description="Download the best-quality audio of every video on YouTube channels, resumably.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('tubeaudio')}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", parents=[common, source, auth], help="list channel videos into pending.txt").set_defaults(
        func=cmd_collect
    )
    sub.add_parser("download", parents=[common, source, auth, download], help="download pending videos").set_defaults(
        func=cmd_download
    )
    sub.add_parser("run", parents=[common, source, auth, download], help="collect, then download").set_defaults(
        func=cmd_run
    )
    sub.add_parser("status", parents=[common], help="show pending/done/failed per channel").set_defaults(
        func=cmd_status
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.data_dir, args.verbose, to_file=args.command != "status")
    try:
        return args.func(args)
    except UsageError as exc:
        log.error("error: %s", exc)
        return 1
    except KeyboardInterrupt:
        # Second Ctrl+C: queue files are always consistent (atomic writes), so exit now
        # without waiting for worker threads.
        log.warning("Interrupted.")
        logging.shutdown()
        os._exit(130)
