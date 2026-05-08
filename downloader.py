from __future__ import annotations

import json
import os
import re
import shutil
import sys
import traceback
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from mutagen.mp4 import MP4, MP4Cover
from PIL import Image, ImageFile, ImageOps
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

ROOT_DIR = Path(__file__).resolve().parent
LINKS_FILE = ROOT_DIR / "links.txt"
TEMP_DIR = ROOT_DIR / ".temp"
MUSIC_DIR = ROOT_DIR / "music"
LOG_DIR = ROOT_DIR / "logs"
ARCHIVE_FILE = LOG_DIR / "downloaded.txt"
ERROR_LOG_FILE = LOG_DIR / "errors.txt"
RUN_LOG_FILE = LOG_DIR / "run.log"
LIBRARY_INDEX_FILE = LOG_DIR / "library_index.json"
LEGACY_ARCHIVE_FILE = ROOT_DIR / "downloaded.txt"

COMMON_TITLE_NOISE = (
    "official video",
    "official music video",
    "official audio",
    "official lyric video",
    "lyric video",
    "lyrics",
    "audio",
    "video",
    "visualizer",
    "topic",
    "hd",
    "4k",
    "remaster",
    "remastered",
    "performance",
    "mv",
)

ImageFile.LOAD_TRUNCATED_IMAGES = True


class TerminalColor:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"


def use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(message: str, color: str) -> str:
    if not use_color():
        return message
    return f"{color}{message}{TerminalColor.RESET}"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_workspace() -> None:
    for directory in (TEMP_DIR, MUSIC_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if not LINKS_FILE.exists():
        LINKS_FILE.write_text("", encoding="utf-8")
    if not ARCHIVE_FILE.exists():
        ARCHIVE_FILE.write_text("", encoding="utf-8")
    if not ERROR_LOG_FILE.exists():
        ERROR_LOG_FILE.write_text("", encoding="utf-8")
    if not RUN_LOG_FILE.exists():
        RUN_LOG_FILE.write_text("", encoding="utf-8")
    if not LIBRARY_INDEX_FILE.exists():
        LIBRARY_INDEX_FILE.write_text("{}", encoding="utf-8")


def write_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def log(message: str) -> None:
    print(message)
    write_line(RUN_LOG_FILE, message)


def status(message: str, color: str) -> None:
    print(paint(message, color))
    write_line(RUN_LOG_FILE, message)


def log_error(link: str, message: str, exc: BaseException | None = None) -> None:
    details = message
    if exc is not None:
        details = f"{message}: {exc}"
    write_line(ERROR_LOG_FILE, f"{link} | {details}")
    if exc is not None:
        write_line(ERROR_LOG_FILE, traceback.format_exc().rstrip())
    write_line(RUN_LOG_FILE, f"ERROR | {link} | {details}")
    print(paint(f"ERROR | {link} | {details}", TerminalColor.RED))


def load_links() -> list[str]:
    seen: set[str] = set()
    ordered_links: list[str] = []
    for raw_line in LINKS_FILE.read_text(encoding="utf-8").splitlines():
        link = raw_line.strip()
        if not link or not link.startswith("http"):
            continue
        if link in seen:
            continue
        seen.add(link)
        ordered_links.append(link)
    return ordered_links


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[\[\(].*?[\]\)]", " ", text)
    text = re.sub(r"\b(?:" + "|".join(re.escape(item) for item in COMMON_TITLE_NOISE) + r")\b", " ", text)
    text = re.sub(r"feat\.?|ft\.?|featuring", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_case_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text


def safe_filename(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[\x00-\x1f\x7f/\\:?*\"<>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or "untitled"


def parse_int_pair(value: Any) -> tuple[int | None, int | None]:
    if value in (None, ""):
        return None, None
    if isinstance(value, (tuple, list)) and value:
        first = int(value[0]) if str(value[0]).isdigit() else None
        second = int(value[1]) if len(value) > 1 and str(value[1]).isdigit() else None
        return first, second
    text = str(value)
    if "/" in text:
        left, right = text.split("/", 1)
        return (int(left) if left.isdigit() else None, int(right) if right.isdigit() else None)
    return (int(text) if text.isdigit() else None, None)


def build_seed_metadata(info: dict[str, Any]) -> dict[str, Any]:
    title = title_case_text(info.get("track") or info.get("title") or "")
    artist = title_case_text(info.get("artist") or info.get("creator") or info.get("uploader") or "")
    album = title_case_text(info.get("album") or "")

    if not artist and " - " in title:
        possible_artist, possible_title = title.split(" - ", 1)
        artist = title_case_text(possible_artist)
        title = title_case_text(possible_title)

    title = re.sub(r"\s*\(.*?\)\s*", " ", title)
    title = re.sub(r"\s*\[.*?\]\s*", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    clean_artist = title_case_text(artist)
    clean_title = title_case_text(title)
    release_date = str(info.get("release_date") or info.get("upload_date") or "")
    year = release_date[:4] if len(release_date) >= 4 else str(info.get("release_year") or "")
    track_number, track_total = parse_int_pair(info.get("track_number"))
    disc_number, disc_total = parse_int_pair(info.get("disc_number"))

    return {
        "title": clean_title,
        "artist": clean_artist,
        "album": album,
        "album_artist": title_case_text(info.get("album_artist") or clean_artist),
        "genre": title_case_text(info.get("genre") or ""),
        "year": year,
        "track_number": track_number,
        "track_total": track_total,
        "disc_number": disc_number,
        "disc_total": disc_total,
        "source_title": title_case_text(info.get("title") or ""),
        "source_url": title_case_text(info.get("webpage_url") or ""),
    }


def score_itunes_result(seed: dict[str, Any], result: dict[str, Any]) -> int:
    score = 0
    seed_title = normalize_text(seed["title"])
    seed_artist = normalize_text(seed["artist"])
    result_title = normalize_text(result.get("trackName"))
    result_artist = normalize_text(result.get("artistName"))
    result_album = normalize_text(result.get("collectionName"))

    if seed_title and seed_title == result_title:
        score += 12
    elif seed_title and (seed_title in result_title or result_title in seed_title):
        score += 7

    if seed_artist and seed_artist == result_artist:
        score += 10
    elif seed_artist and (seed_artist in result_artist or result_artist in seed_artist):
        score += 5

    if seed["album"]:
        seed_album = normalize_text(seed["album"])
        if seed_album and (seed_album == result_album or seed_album in result_album or result_album in seed_album):
            score += 3

    if result.get("primaryGenreName"):
        score += 1

    return score


def fetch_json(url: str) -> dict[str, Any] | None:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def search_itunes(seed: dict[str, Any]) -> dict[str, Any] | None:
    query_parts = [seed["artist"], seed["title"]]
    query = " ".join(part for part in query_parts if part).strip()
    if not query:
        return None

    api_url = f"https://itunes.apple.com/search?term={quote_plus(query)}&entity=song&limit=10&country=US"
    try:
        payload = fetch_json(api_url)
    except Exception:
        return None

    results = payload.get("results", []) if payload else []
    if not results:
        return None

    ranked = sorted(results, key=lambda item: score_itunes_result(seed, item), reverse=True)
    best = ranked[0]
    if score_itunes_result(seed, best) < 4:
        return None
    return best


def candidate_artwork_urls(result: dict[str, Any] | None, fallback_thumbnail: str | None) -> list[str]:
    candidates: list[str] = []
    if result:
        for key in ("artworkUrl100", "artworkUrl60"):
            value = result.get(key)
            if not value:
                continue
            text = str(value)
            candidates.append(text)
            candidates.append(text.replace("100x100bb", "1000x1000bb").replace("60x60bb", "1000x1000bb"))
            candidates.append(text.replace("100x100bb", "600x600bb").replace("60x60bb", "600x600bb"))
            candidates.append(text.replace("100x100", "1000x1000").replace("60x60", "1000x1000"))
    if fallback_thumbnail:
        candidates.append(fallback_thumbnail)
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def download_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        return response.read()


def square_cover_art(image_bytes: bytes) -> bytes:
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    image = ImageOps.fit(image, (1000, 1000), method=Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95, optimize=True)
    return buffer.getvalue()


def fetch_cover_art(seed: dict[str, Any], itunes_result: dict[str, Any] | None, fallback_thumbnail: str | None) -> bytes | None:
    for candidate in candidate_artwork_urls(itunes_result, fallback_thumbnail):
        try:
            image_bytes = download_bytes(candidate)
            return square_cover_art(image_bytes)
        except Exception:
            continue
    return None


def rebuild_library_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not MUSIC_DIR.exists():
        return index

    for audio_path in MUSIC_DIR.glob("*.m4a"):
        try:
            audio = MP4(audio_path)
        except Exception:
            continue

        title_values = audio.tags.get("\xa9nam") if audio.tags else None
        artist_values = audio.tags.get("\xa9ART") if audio.tags else None
        album_artist_values = audio.tags.get("aART") if audio.tags else None

        title = str((title_values or [audio_path.stem])[0])
        artist = str((artist_values or album_artist_values or [""])[0])
        key = canonical_key(artist, title)
        if key:
            index[key] = {"path": audio_path.name, "artist": artist, "title": title}
    return index


def load_library_index() -> dict[str, dict[str, Any]]:
    on_disk = rebuild_library_index()
    try:
        saved = json.loads(LIBRARY_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    if isinstance(saved, dict):
        for key, value in saved.items():
            if isinstance(value, dict) and key not in on_disk:
                on_disk[key] = value
    save_library_index(on_disk)
    return on_disk


def save_library_index(index: dict[str, dict[str, Any]]) -> None:
    LIBRARY_INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def canonical_key(artist: Any, title: Any) -> str:
    normalized_artist = normalize_text(artist)
    normalized_title = normalize_text(title)
    if not normalized_artist and not normalized_title:
        return ""
    return f"{normalized_artist}::{normalized_title}"


def load_archived_ids() -> set[str]:
    archived_ids: set[str] = set()
    for archive_path in (ARCHIVE_FILE, LEGACY_ARCHIVE_FILE):
        if not archive_path.exists():
            continue
        for raw_line in archive_path.read_text(encoding="utf-8").splitlines():
            parts = raw_line.strip().split(maxsplit=1)
            if len(parts) == 2:
                archived_ids.add(parts[1])
    return archived_ids


def append_archive_id(source_id: str) -> None:
    line = f"youtube {source_id}"
    existing_lines = set(ARCHIVE_FILE.read_text(encoding="utf-8").splitlines()) if ARCHIVE_FILE.exists() else set()
    if line not in existing_lines:
        with ARCHIVE_FILE.open("a", encoding="utf-8") as handle:
            if existing_lines:
                handle.write("\n")
            handle.write(line)


def locate_downloaded_file(job_dir: Path) -> Path | None:
    audio_files = [path for path in job_dir.iterdir() if path.is_file() and path.suffix.lower() in {".m4a", ".mp4", ".webm", ".opus", ".mkv"}]
    if not audio_files:
        return None
    audio_files.sort(key=lambda item: item.stat().st_size, reverse=True)
    return audio_files[0]


def build_ydl_options(job_dir: Path) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 20,
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "download_archive": str(ARCHIVE_FILE),
    }
    if shutil.which("ffmpeg"):
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }
        ]
    else:
        options["format"] = "bestaudio[ext=m4a]/ba[ext=m4a]"
    return options


def enrich_metadata(info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    seed = build_seed_metadata(info)
    itunes_result = search_itunes(seed)

    if itunes_result:
        seed.update(
            {
                "title": title_case_text(itunes_result.get("trackName") or seed["title"]),
                "artist": title_case_text(itunes_result.get("artistName") or seed["artist"]),
                "album": title_case_text(itunes_result.get("collectionName") or seed["album"]),
                "album_artist": title_case_text(itunes_result.get("collectionArtistName") or itunes_result.get("artistName") or seed["album_artist"]),
                "genre": title_case_text(itunes_result.get("primaryGenreName") or seed["genre"]),
            }
        )
        release_date = str(itunes_result.get("releaseDate") or "")
        if len(release_date) >= 4:
            seed["year"] = release_date[:4]
        track_number, track_total = parse_int_pair(itunes_result.get("trackNumber"))
        disc_number, disc_total = parse_int_pair(itunes_result.get("discNumber"))
        if track_number is not None:
            seed["track_number"] = track_number
        if track_total is not None:
            seed["track_total"] = track_total
        if disc_number is not None:
            seed["disc_number"] = disc_number
        if disc_total is not None:
            seed["disc_total"] = disc_total

    return seed, itunes_result


def embed_metadata(audio_path: Path, metadata: dict[str, Any], cover_bytes: bytes | None) -> None:
    audio = MP4(audio_path)
    tags = audio.tags or {}
    tags["\xa9nam"] = [metadata["title"]]
    tags["\xa9ART"] = [metadata["artist"]]
    tags["\xa9alb"] = [metadata["album"] or metadata["title"]]
    tags["aART"] = [metadata["album_artist"] or metadata["artist"]]
    if metadata.get("genre"):
        tags["\xa9gen"] = [metadata["genre"]]
    if metadata.get("year"):
        tags["\xa9day"] = [metadata["year"]]

    track_number = metadata.get("track_number")
    track_total = metadata.get("track_total") or 0
    if track_number:
        tags["trkn"] = [(int(track_number), int(track_total))]

    disc_number = metadata.get("disc_number")
    disc_total = metadata.get("disc_total") or 0
    if disc_number:
        tags["disk"] = [(int(disc_number), int(disc_total))]

    if cover_bytes:
        tags["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.tags = tags
    audio.save()


def build_final_path(metadata: dict[str, Any]) -> Path:
    song_label = safe_filename(f"{metadata['artist']} - {metadata['title']}")
    base_name = f"{song_label}.m4a"
    candidate = MUSIC_DIR / base_name
    suffix = 2
    while candidate.exists():
        candidate = MUSIC_DIR / f"{song_label} ({suffix}).m4a"
        suffix += 1
    return candidate


def cleanup_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def process_link(link: str, archived_ids: set[str], library_index: dict[str, dict[str, Any]]) -> None:
    job_dir = TEMP_DIR / safe_filename(str(hash(link)))
    cleanup_directory(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as probe:
            info = probe.extract_info(link, download=False)

        source_id = str(info.get("id") or "")
        if source_id and source_id in archived_ids:
            status(f"SKIP archive hit: {link}", TerminalColor.YELLOW)
            return

        metadata, itunes_result = enrich_metadata(info)
        media_key = canonical_key(metadata["artist"], metadata["title"])
        if media_key and media_key in library_index:
            status(f"SKIP duplicate library entry: {metadata['artist']} - {metadata['title']}", TerminalColor.YELLOW)
            if source_id and source_id not in archived_ids:
                append_archive_id(source_id)
                archived_ids.add(source_id)
            return

        status(f"START {metadata['artist']} - {metadata['title']}", TerminalColor.CYAN)

        with YoutubeDL(build_ydl_options(job_dir)) as downloader:
            downloader.download([link])

        downloaded_file = locate_downloaded_file(job_dir)
        if not downloaded_file or downloaded_file.suffix.lower() != ".m4a":
            raise RuntimeError("download finished without a final .m4a file")

        cover_bytes = fetch_cover_art(metadata, itunes_result, str(info.get("thumbnail") or ""))
        embed_metadata(downloaded_file, metadata, cover_bytes)

        final_path = build_final_path(metadata)
        shutil.move(str(downloaded_file), str(final_path))

        if source_id:
            append_archive_id(source_id)
            archived_ids.add(source_id)

        if media_key:
            library_index[media_key] = {
                "path": final_path.name,
                "artist": metadata["artist"],
                "title": metadata["title"],
                "album": metadata.get("album", ""),
            }
            save_library_index(library_index)

        status(f"DONE  {final_path.name}", TerminalColor.GREEN)

    except DownloadError as exc:
        log_error(link, "download failed", exc)
        raise
    except Exception as exc:
        log_error(link, "processing failed", exc)
        raise
    finally:
        cleanup_directory(job_dir)


def main() -> int:
    ensure_workspace()
    links = load_links()
    if not links:
        print(paint("No valid links found in links.txt", TerminalColor.YELLOW))
        return 1

    archived_ids = load_archived_ids()
    library_index = load_library_index()

    print(paint(f"\nFound {len(links)} unique links.\n", TerminalColor.BOLD))

    for link in links:
        print(paint("=" * 60, TerminalColor.BOLD))
        print(paint(f"Processing: {link}", TerminalColor.CYAN))
        print(paint("=" * 60, TerminalColor.BOLD))
        try:
            process_link(link, archived_ids, library_index)
        except Exception:
            print(paint(f"\nFailed:\n{link}\n", TerminalColor.RED))

    print(paint("\nAll downloads completed.\n", TerminalColor.GREEN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())