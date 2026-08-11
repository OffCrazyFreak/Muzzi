#!/usr/bin/env python3
"""Read-only survey of a music folder. Touches nothing, writes one JSON report.

Usage: python3 survey.py "/path/to/music" [-o report.json]
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter

from mutagen import File as MutagenFile

AUDIO_EXT = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav", ".aac", ".wma"}

# Cruft that YouTube rips carry in the title.
CRUFT = re.compile(
    r"""\s*(?:
        [\(\[]\s*(?:official\s*)?(?:music\s*)?(?:lyrics?|lyric|video|audio|visualizer|
            hd|hq|4k|full\s*hd|mv|clip|version|remaster(?:ed)?(?:\s*\d{4})?)\s*
            [^\)\]]*[\)\]]
      | [\(\[]\s*(?:official|explicit|clean|uncensored)\s*[^\)\]]*[\)\]]
      | \b(?:official\s+video|official\s+audio|lyrics?\s+video|music\s+video)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# "Artist - Title" with common separator variants.
SPLIT = re.compile(r"\s+[-–—]\s+|\s+[-–—]|[-–—]\s+")

# Characters specific to BCMS (Bosnian/Croatian/Montenegrin/Serbian) orthography.
BCMS_CHARS = set("čćšžđČĆŠŽĐ")
# Cyrillic block, for Serbian-script titles.
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
# yt-dlp substitutes fullwidth forms for characters illegal in filenames.
FULLWIDTH = set("？＂＊：＜＞｜／＼")


def strip_cruft(name):
    prev = None
    out = name
    while out != prev:
        prev = out
        out = CRUFT.sub(" ", out)
    return re.sub(r"\s{2,}", " ", out).strip(" -–—_")


def guess_artist_title(stem):
    """Return (artist, title, confidence) from a filename stem."""
    cleaned = strip_cruft(stem)
    parts = [p.strip() for p in SPLIT.split(cleaned) if p.strip()]
    if len(parts) >= 2:
        # Treat everything after the first separator as the title, so
        # "A x B - Song - Live" keeps its trailing qualifier.
        return parts[0], " - ".join(parts[1:]), "high"
    if cleaned:
        return None, cleaned, "low"
    return None, stem, "none"


def tag_snapshot(path):
    """Existing tags, normalised across containers. None if unreadable."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return None
    if audio is None:
        return None

    def first(key):
        val = (audio.tags or {}).get(key)
        if not val:
            return None
        v = val[0] if isinstance(val, list) else val
        v = str(v).strip()
        return v or None

    bpm = first("bpm")
    try:
        bpm = float(bpm) if bpm else None
    except ValueError:
        bpm = None

    info = getattr(audio, "info", None)
    return {
        "artist": first("artist"),
        "title": first("title"),
        "album": first("album"),
        "albumartist": first("albumartist"),
        "date": first("date"),
        "genre": first("genre"),
        "bpm": bpm,
        "key": first("initialkey"),
        "musicbrainz_trackid": first("musicbrainz_trackid"),
        "duration": round(info.length, 1) if info and getattr(info, "length", None) else None,
        "bitrate": getattr(info, "bitrate", None) if info else None,
    }


def has_cover(path):
    """Only meaningful for ID3; other containers report None."""
    if not path.lower().endswith(".mp3"):
        return None
    try:
        raw = MutagenFile(path)
        return bool(raw and raw.tags and raw.tags.getall("APIC"))
    except Exception:
        return None


def looks_balkan(text):
    if not text:
        return False
    if CYRILLIC.search(text):
        return True
    return any(c in BCMS_CHARS for c in text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("-o", "--out", default="survey_report.json")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"not a directory: {root}")

    tracks = []
    for dirpath, _, filenames in os.walk(root):
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in AUDIO_EXT:
                continue
            full = os.path.join(dirpath, fn)
            stem = os.path.splitext(fn)[0]
            artist, title, conf = guess_artist_title(stem)
            tags = tag_snapshot(full)
            tracks.append({
                "path": os.path.relpath(full, root),
                "ext": ext,
                "size": os.path.getsize(full),
                "filename_artist": artist,
                "filename_title": title,
                "parse_confidence": conf,
                "cruft_stripped": strip_cruft(stem) != stem,
                "mojibake_chars": sorted(set(fn) & FULLWIDTH) or None,
                "tags": tags,
                "has_cover": has_cover(full),
                # Filename and tags are separate evidence; either can flag it.
                "likely_balkan": looks_balkan(stem) or looks_balkan(
                    " ".join(filter(None, [
                        (tags or {}).get("artist"), (tags or {}).get("title")
                    ]))
                ),
            })

    total = len(tracks)
    if not total:
        sys.exit(f"no audio files under {root}")

    def count(pred):
        return sum(1 for t in tracks if pred(t))

    def tagged(t, field):
        return bool((t["tags"] or {}).get(field))

    summary = {
        "root": root,
        "total_tracks": total,
        "total_bytes": sum(t["size"] for t in tracks),
        "unreadable": count(lambda t: t["tags"] is None),
        "by_extension": dict(Counter(t["ext"] for t in tracks).most_common()),
        "tags_present": {
            f: count(lambda t, f=f: tagged(t, f))
            for f in ["artist", "title", "album", "albumartist", "date", "genre"]
        },
        "has_bpm": count(lambda t: (t["tags"] or {}).get("bpm") is not None),
        "has_key": count(lambda t: tagged(t, "key")),
        "has_cover": count(lambda t: t["has_cover"]),
        "has_musicbrainz_id": count(lambda t: tagged(t, "musicbrainz_trackid")),
        "no_tags_at_all": count(
            lambda t: t["tags"] is not None
            and not any(tagged(t, f) for f in ["artist", "title", "album"])
        ),
        "filename_parse": dict(Counter(t["parse_confidence"] for t in tracks).most_common()),
        "had_youtube_cruft": count(lambda t: t["cruft_stripped"]),
        "mojibake_filenames": count(lambda t: t["mojibake_chars"]),
        "likely_balkan": count(lambda t: t["likely_balkan"]),
        "duplicate_stems": [
            stem for stem, n in Counter(
                unicodedata.normalize("NFKD", os.path.splitext(os.path.basename(t["path"]))[0]).lower()
                for t in tracks
            ).items() if n > 1
        ],
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "tracks": tracks}, fh,
                  ensure_ascii=False, indent=2)

    pct = lambda n: f"{n:5d}  ({100 * n / total:4.1f}%)"
    print(f"\n  {total} tracks, {summary['total_bytes'] / 1e9:.2f} GB in {root}\n")
    print("  EXISTING TAGS")
    for f, n in summary["tags_present"].items():
        print(f"    {f:<12} {pct(n)}")
    print(f"    {'bpm':<12} {pct(summary['has_bpm'])}")
    print(f"    {'key':<12} {pct(summary['has_key'])}")
    print(f"    {'cover art':<12} {pct(summary['has_cover'])}")
    print(f"    {'MBID':<12} {pct(summary['has_musicbrainz_id'])}")
    print(f"\n    completely untagged  {pct(summary['no_tags_at_all'])}")
    print(f"    unreadable files     {pct(summary['unreadable'])}")
    print("\n  FILENAME EVIDENCE")
    print(f"    parses as 'Artist - Title'  {pct(summary['filename_parse'].get('high', 0))}")
    print(f"    title only, no artist       {pct(summary['filename_parse'].get('low', 0))}")
    print(f"    carried YouTube cruft       {pct(summary['had_youtube_cruft'])}")
    print(f"    yt-dlp fullwidth chars      {pct(summary['mojibake_filenames'])}")
    print(f"    likely Balkan (BCMS/Cyr)    {pct(summary['likely_balkan'])}")
    if summary["duplicate_stems"]:
        print(f"\n    duplicate filenames: {len(summary['duplicate_stems'])}")
    print(f"\n  full report -> {args.out}\n")


if __name__ == "__main__":
    main()
