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

| Option | Description |
|---|---|
| `--channel @handle` | process only this channel (repeatable; skips `channels.txt`) |
| `--workers N` | parallel downloads (default `1`) |
| `--limit N` | stop after N new downloads |
| `--sleep MIN MAX` | random pause between downloads in seconds (default `2 6`) |
| `--cookies FILE`, `--cookies-from-browser NAME` | use your YouTube login |
| `--retry-failed` | re-queue videos that failed permanently |
| `--data-dir DIR` | output directory (default `data`) |

## Output

```
data/channels/<channel>/
├── pending.txt      # URLs left to download
├── done.txt         # downloaded video IDs
├── failed.tsv       # unavailable videos and the reason
└── audio/
    ├── <id>.opus
    └── <id>.info.json
```

## Development

```bash
uv run pytest
uv run ruff check .
```

Only download content you have the right to use, and respect YouTube's Terms of Service.

## License

[MIT](LICENSE)
