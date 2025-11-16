#!/usr/bin/env python3
"""
Recursively search a music library for audio files, fetch lyrics from LRCLIB,
and create .lrc files with the same base name as each track.

Supported formats: .flac, .mp3, .m4a, .aac, .ogg, .wav (configurable).
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests
from mutagen import File as MutagenFile


# ---- Config -----------------------------------------------------------------

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".wav"}
LRCLIB_BASE_URL = "https://lrclib.net"
REQUEST_TIMEOUT = 15  # seconds
SLEEP_BETWEEN_REQUESTS = 0.3  # be gentle to the API


# ---- Helpers ----------------------------------------------------------------


def normalize(s: str) -> str:
    """Normalize a string for fuzzy comparison."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def infer_from_path(path: Path, library_root: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Infer artist, title, album from the file path, given a library structured like:
    artist/album[/CD1]/song.ext
    """
    artist = album = title = None

    try:
        rel_parts = path.relative_to(library_root).parts
    except ValueError:
        # Fallback if the path is not under library_root for some reason
        rel_parts = path.parts

    if len(rel_parts) >= 1:
        artist = rel_parts[0]

    if len(rel_parts) >= 2:
        album = rel_parts[1]

        # Handle artist/album/CD1/song
        if len(rel_parts) >= 3 and re.match(r"^cd\s*\d+$", rel_parts[2], re.IGNORECASE):
            album = f"{album} {rel_parts[2]}"

    # Title from filename
    stem = path.stem
    stem_clean = stem.replace("_", " ")

    # Strip track number prefixes like "01 - Song Name" or "01.Song Name"
    m = re.match(r"^\s*\d+\s*[-_.]\s*(.+)$", stem_clean)
    if m:
        title = m.group(1).strip()
    else:
        title = stem_clean.strip()

    # Sometimes filename is "Artist - Title"
    if " - " in stem_clean and not artist:
        maybe_artist, maybe_title = stem_clean.split(" - ", 1)
        artist = maybe_artist.strip()
        title = maybe_title.strip() or title

    return artist or None, title or None, album or None


def get_metadata(path: Path, library_root: Path) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """
    Read metadata (artist, title, album, duration) using mutagen.
    Fall back to path-based inference where needed.
    """
    artist = title = album = None
    duration = None

    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        audio = None

    if audio is not None:
        tags = audio.tags or {}

        def first(key: str) -> Optional[str]:
            v = tags.get(key)
            if isinstance(v, list) and v:
                return str(v[0])
            if isinstance(v, str):
                return v
            return None

        title = first("title")
        artist = first("artist")
        album = first("album")

        try:
            if audio.info and hasattr(audio.info, "length"):
                duration = int(round(audio.info.length))
        except Exception:
            duration = None

    # Fill in gaps with path inference
    inf_artist, inf_title, inf_album = infer_from_path(path, library_root)

    if not artist:
        artist = inf_artist
    if not title:
        title = inf_title
    if not album:
        album = inf_album

    return artist, title, album, duration


def choose_best_result(results, title: Optional[str], artist: Optional[str]):
    """
    Pick the best LRCLIB result based on normalized title/artist.
    """
    if not results:
        return None

    if not title and not artist:
        return results[0]

    norm_title = normalize(title or "")
    norm_artist = normalize(artist or "")

    best = None
    for r in results:
        r_title = normalize(r.get("trackName") or r.get("name") or "")
        r_artist = normalize(r.get("artistName") or "")

        title_match = norm_title and r_title == norm_title
        artist_match = norm_artist and r_artist == norm_artist

        # Prefer both matching, then title-only, then artist-only
        score = (2 if title_match and artist_match else
                 1 if title_match or artist_match else
                 0)

        if best is None or score > best[0]:
            best = (score, r)

    return best[1] if best else results[0]


def fetch_lyrics_from_lrclib(
    session: requests.Session,
    artist: Optional[str],
    title: Optional[str],
    album: Optional[str],
    duration: Optional[int],
    logger: logging.Logger,
) -> Tuple[Optional[str], bool]:
    """
    Fetch lyrics from LRCLIB.

    Returns (lyrics_text, is_synced), where is_synced indicates that lyrics
    are already in LRC-ish format with timestamps.
    """
    if not title and not artist:
        return None, False

    params = {}

    # You can either use `q` or more specific params; `q` is simple and works well.
    # See https://lrclib.net/docs :contentReference[oaicite:1]{index=1}
    if artist and title:
        params["q"] = f"{artist} {title}"
    elif title:
        params["q"] = title
    elif artist:
        params["q"] = artist

    url = f"{LRCLIB_BASE_URL}/api/search"

    try:
        resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        logger.warning("LRCLIB request failed for %s - %s: %s", artist, title, e)
        return None, False

    if not isinstance(results, list) or not results:
        return None, False

    best = choose_best_result(results, title, artist)
    if not best:
        return None, False

    if best.get("instrumental"):
        logger.info("Instrumental track (no lyrics) according to LRCLIB.")
        return None, False

    synced = best.get("syncedLyrics") or ""
    plain = best.get("plainLyrics") or ""

    if synced.strip():
        return synced, True
    if plain.strip():
        return plain, False

    return None, False


def make_unsynced_lrc(plain_lyrics: str) -> str:
    """
    Turn plain text lyrics into a simple unsynced .lrc:
    we just stamp each line with [00:00.00].
    """
    lines = plain_lyrics.splitlines()
    out_lines = []
    for line in lines:
        if line.strip():
            out_lines.append(f"[00:00.00] {line}")
        else:
            out_lines.append("")
    return "\n".join(out_lines).rstrip() + "\n"


def write_lrc_for_track(
    track_path: Path,
    lyrics: str,
    is_synced: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> None:
    """
    Write the .lrc file next to the track with same basename.
    """
    lrc_path = track_path.with_suffix(".lrc")

    if lrc_path.exists() and not overwrite:
        logger.info("Skipping existing .lrc: %s", lrc_path)
        return

    if is_synced:
        content = lyrics.rstrip() + "\n"
    else:
        content = make_unsynced_lrc(lyrics)

    try:
        lrc_path.write_text(content, encoding="utf-8")
        logger.info("Wrote %s", lrc_path)
    except Exception as e:
        logger.error("Failed to write %s: %s", lrc_path, e)


# ---- Main logic -------------------------------------------------------------


def process_library(root: Path, overwrite: bool, logger: logging.Logger) -> None:
    session = requests.Session()

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        # Skip macOS resource-fork files like ._track.mp3
        if path.name.startswith("._"):
            continue

        if path.suffix.lower() not in AUDIO_EXTS:
            continue

        lrc_path = path.with_suffix(".lrc")
        if lrc_path.exists() and not overwrite:
            logger.debug("LRC exists, skipping: %s", path)
            continue

        artist, title, album, duration = get_metadata(path, root)
        logger.info(
            "Processing: %s (artist=%r, title=%r, album=%r, duration=%r)",
            path, artist, title, album, duration,
        )

        lyrics, is_synced = fetch_lyrics_from_lrclib(
            session=session,
            artist=artist,
            title=title,
            album=album,
            duration=duration,
            logger=logger,
        )

        if not lyrics:
            logger.warning("No lyrics found for %s", path)
            continue

        write_lrc_for_track(path, lyrics, is_synced, overwrite, logger)

        # Be nice to the API
        time.sleep(SLEEP_BETWEEN_REQUESTS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Recursively fetch lyrics for a music library and create .lrc files."
    )
    parser.add_argument(
        "root",
        type=Path,
        help="Root directory of your music library (artist/album[/CD1]/song).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .lrc files.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    logger = logging.getLogger("lyrics_to_lrc")

    logger.info("Scanning library at %s", root)
    logger.info("Audio extensions: %s", ", ".join(sorted(AUDIO_EXTS)))
    process_library(root, overwrite=args.overwrite, logger=logger)
    logger.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
