# tubeaudio

[![CI](https://github.com/DovudAsadov/tubeaudio/actions/workflows/ci.yml/badge.svg)](https://github.com/DovudAsadov/tubeaudio/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Download the best-quality audio of every video on one or more YouTube channels.
Built for collecting speech and audio datasets.

- **Original quality**: saves YouTube's best audio stream without re-encoding (usually Opus, 48 kHz), plus metadata JSON
- **Resumable**: the queue lives in plain text files, so you can stop at any time and rerun to continue
- **Incremental**: rerunning `collect` only adds new uploads
- **Robust**: retries with backoff, stops cleanly on rate limits, never saves degraded fallback formats
- **Disk-safe**: stops before the disk fills up; free some space and rerun to continue

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), `ffmpeg`, and `deno` or `node`.

```bash
git clone https://github.com/DovudAsadov/tubeaudio.git
cd tubeaudio
uv sync
```

## Usage

```bash
cp channels.example.txt channels.txt   # one channel per line: @handle, URL, or UC… ID
uv run tubeaudio run                   # collect video URLs, then download audio
uv run tubeaudio status                # pending / done / failed per channel
```

The two steps can also run separately: `tubeaudio collect`, then `tubeaudio download`.
When a channel is fully downloaded, `run` comments it out in `channels.txt`; remove the `# ` to fetch its new uploads.

| Option | Description |
|---|---|
| `--channel @handle` | process only this channel (repeatable; skips `channels.txt`) |
| `--workers N` | parallel downloads (default `1`) |
| `--limit N` | stop after N new downloads |
| `--sleep MIN MAX` | random pause between downloads in seconds (default `2 6`) |
| `--cookies FILE`, `--cookies-from-browser NAME` | use your YouTube login |
| `--retry-failed` | re-queue videos that failed permanently |
| `--min-free-gb GB` | stop when free disk space drops below this (default `2`, `0` disables) |
| `--data-dir DIR` | output directory (default `data`) |

## Output

```
data/channels/<channel>/
├── pending.txt      # URLs left to download
├── done.txt         # URLs already downloaded
├── failed.tsv       # unavailable videos and the reason
└── audio/
    ├── <id>.opus
    └── <id>.info.json
```

Finished `.opus`/`.info.json` files can be moved elsewhere at any time (e.g. when the disk is full); they are never downloaded again.

## Development

```bash
uv run pytest
uv run ruff check .
```

Only download content you have the right to use, and respect YouTube's Terms of Service.

## License

[MIT](LICENSE)
