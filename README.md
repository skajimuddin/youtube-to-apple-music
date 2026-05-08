# Local Music Downloader

## Motivation

I needed my entire music playlist local and offline on my iPhone without needing a subscription.So i made This tool it downloads music from YouTube, automatically sets up professional metadata (artist, title, album, cover art), and creates properly tagged m4a files. Then I add these files to Apple Music on my PC and sync them to my iPhone using the Apple Devices app—boom, offline music without paying for a subscription.

## Overview

Download audio into a staged local library with better metadata, square cover art, and safe temp processing.

## Layout

- `links.txt` - input URLs, one per line
- `.temp/` - transient processing workspace
- `music/` - final library output
- `logs/downloaded.txt` - processed source archive
- `logs/errors.txt` - failure log
- `logs/library_index.json` - duplicate protection index

## How It Works

1. Each link is extracted with `yt-dlp`.
2. Metadata is enriched from iTunes when a clean match is found.
3. Cover art is fetched and normalized to a square JPEG.
4. Tags are written into the file with `mutagen`.
5. Only after tagging succeeds is the file moved into `music/`.

If `ffmpeg` is available, the downloader can convert non-m4a audio. Without `ffmpeg`, it stays on m4a sources so the final files remain valid.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python downloader.py
```

## Notes

- Add your own links to `links.txt`.
- Keep secrets out of the repository. Use environment variables if you add API-backed features later.