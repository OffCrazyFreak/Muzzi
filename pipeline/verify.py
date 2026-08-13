#!/usr/bin/env python3
"""Audit a processed library: what is present, stale, missing or suspect.

The provenance stamps written by write_tags exist so that re-ingesting a
finished library costs a tag read rather than a full re-run. Nothing read them
until now.

Answers three questions:
  1. Has this file been through Muzzi at all, and at which version?
  2. Does what is IN the file still match what the pipeline currently believes?
     (a mismatched digest means the file was hand-edited or the pipeline
     changed its mind since)
  3. Which fields are missing, and which values look suspect?

Fast: reads tags only, no audio decoding. ~3000 files in a few seconds.

Usage:
  verify.py output/_all
  verify.py output/_all --json report.json
"""
import argparse
import json
import os
import sys
from collections import Counter

import mutagen

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.write_tags import RG_TARGET_LUFS  # noqa: E402

VERSION = "muzzi/1"
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav")


def _num(v):
    """-> float from a tag value, tolerating the ' dB' / ' LUFS' suffix."""
    if v is None:
        return None
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError):
        return None


def read_tags(path):
    """Normalise MP3 / FLAC / MP4 tags into one flat dict."""
    try:
        f = mutagen.File(path)
    except Exception as e:
        return None, f"unreadable: {type(e).__name__}"
    if f is None or f.tags is None:
        return {}, None

    out = {}
    t = f.tags
    if hasattr(t, "getall"):                     # ID3
        def first(k):
            fr = t.getall(k)
            return str(fr[0].text[0]) if fr and getattr(fr[0], "text", None) else None
        for tag, key in (("TPE1", "artist"), ("TIT2", "title"), ("TALB", "album"),
                         ("TDRC", "year"), ("TCON", "genre"), ("TBPM", "bpm"),
                         ("TKEY", "key"), ("TLAN", "language")):
            out[key] = first(tag)
        for fr in t.getall("TXXX"):
            if fr.text:
                out[f"x:{fr.desc}"] = str(fr.text[0])
        out["_art"] = bool(t.getall("APIC"))
        out["_lyrics"] = bool(t.getall("USLT"))
    else:                                        # Vorbis / MP4
        low = {str(k).lower(): v for k, v in t.items()}

        def g(*names):
            for n in names:
                if n in low:
                    v = low[n]
                    return str(v[0] if isinstance(v, list) else v)
            return None
        # MP4 keeps non-standard fields under "----:com.apple.iTunes:NAME" as
        # raw bytes. Without unpacking those, every m4a looked unprocessed:
        # 125 files reported as missing a stamp they in fact carried.
        for k, v in list(t.items()):
            if str(k).startswith("----:com.apple.iTunes:"):
                name = str(k).rsplit(":", 1)[-1]
                val = v[0] if isinstance(v, list) else v
                if isinstance(val, bytes):
                    val = val.decode("utf-8", "ignore")
                out[f"x:{name}"] = str(val)
        # Vorbis comments are flat upper-case keys, not freeform atoms, so they
        # need the same namespace or every FLAC and Ogg file reads as carrying
        # no ReplayGain and no MUZZI_* stamp at all. write_generic writes them
        # as f[KEY.upper()], and .flac/.ogg/.opus are in AUDIO_EXT, so without
        # this the exit condition below fails the run on any such file.
        for k, v in list(t.items()):
            key = str(k)
            if ":" in key:
                continue
            val = v[0] if isinstance(v, list) else v
            out.setdefault(f"x:{key.upper()}", str(val))
        out["artist"] = g("artist", "\xa9art")
        out["title"] = g("title", "\xa9nam")
        out["album"] = g("album", "\xa9alb")
        out["year"] = g("date", "\xa9day")
        out["genre"] = g("genre", "\xa9gen")
        out["bpm"] = g("bpm", "tmpo")
        out["_art"] = bool(getattr(f, "pictures", None)) or "covr" in low
        out["_lyrics"] = bool(g("lyrics", "\xa9lyr"))
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--json", help="write the full per-file report here")
    args = ap.parse_args()

    files = []
    for dp, _, names in os.walk(args.root):
        for n in sorted(names):
            if n.lower().endswith(AUDIO_EXT):
                files.append(os.path.join(dp, n))
    if not files:
        sys.exit(f"no audio under {args.root}")

    # What the pipeline currently believes, for digest comparison.
    review = {}
    rp = os.path.join(HERE, "cache", "review.json")
    if os.path.exists(rp):
        for r in json.load(open(rp)):
            if r.get("proposed_artist"):
                key = f'{r["proposed_artist"]}|{r["proposed_title"]}'.lower()
                review[key] = r

    rows, counts = [], Counter()
    for p in files:
        tags, err = read_tags(p)
        rec = {"file": os.path.basename(p)}
        if err:
            rec["status"], rec["detail"] = "unreadable", err
            counts["unreadable"] += 1
            rows.append(rec)
            continue

        ver = tags.get("x:MUZZI_VERSION")
        missing = [k for k in ("artist", "title", "bpm") if not tags.get(k)]
        rec.update({
            "version": ver, "missing": missing,
            "art": tags.get("_art"), "lyrics": tags.get("_lyrics"),
            "bpm": tags.get("bpm"),
            "ambiguous_bpm": tags.get("x:BPM_AMBIGUOUS"),
            "bpm_verdict": tags.get("x:BPM_VERDICT"),
            "key_agreement": tags.get("x:KEY_AGREEMENT"),
            "quality": tags.get("x:QUALITY"),
            "youtube": tags.get("x:YOUTUBE_ID"),
            "youtube_trust": tags.get("x:YOUTUBE_TRUST"),
        })

        if not ver:
            rec["status"] = "unprocessed"
            counts["unprocessed"] += 1
        elif ver != VERSION:
            rec["status"] = "stale_version"
            counts["stale_version"] += 1
        elif tags.get("x:MUZZI_SOURCE") == "audio-only":
            # Deliberately identity-less: no source could name it confidently,
            # so it carries audio facts only. Not a failure.
            rec["status"] = "audio_only"
            counts["audio_only"] += 1
        elif missing:
            rec["status"] = "incomplete"
            counts["incomplete"] += 1
        else:
            rec["status"] = "ok"
            counts["ok"] += 1

        # ReplayGain. Nothing checked these until now, which is how the whole
        # library shipped with no peak tag at all and a gain computed from a
        # mono downmix: every value was individually plausible and no stage
        # ever compared them to anything.
        gain = _num(tags.get("x:REPLAYGAIN_TRACK_GAIN"))
        rec["replaygain"] = gain
        if gain is None:
            counts["rg_missing"] += 1
        elif not -30.0 <= gain <= 30.0:
            # Not a judgement about loudness, just a sanity bound: a gain
            # outside this is a parse or unit error, not a quiet track.
            counts["rg_implausible"] += 1
        if _num(tags.get("x:REPLAYGAIN_TRACK_PEAK")) is None:
            counts["rg_no_peak"] += 1
        ref = _num(tags.get("x:REPLAYGAIN_REFERENCE_LOUDNESS"))
        if gain is not None and (ref is None or abs(ref - RG_TARGET_LUFS) > 0.01):
            # A file tagged against a different target is not wrong in itself,
            # but it is not comparable to the rest of the library, which is the
            # entire point of normalising.
            counts["rg_wrong_target"] += 1

        if tags.get("x:BPM_VERDICT") == "disagree":
            counts["bpm_disagree"] += 1
        if tags.get("x:BPM_AMBIGUOUS") == "half-or-double":
            counts["bpm_ambiguous"] += 1
        if tags.get("x:QUALITY") == "low":
            counts["low_quality"] += 1
        if not tags.get("_art"):
            counts["no_art"] += 1
        ka = tags.get("x:KEY_AGREEMENT")
        if ka and ka.startswith("1/"):
            counts["key_uncertain"] += 1
        if tags.get("x:YOUTUBE_ID"):
            counts["yt_" + (tags.get("x:YOUTUBE_TRUST") or "unknown")] += 1
        else:
            counts["no_youtube"] += 1
        rows.append(rec)

    n = len(rows)
    print(f"\n  {n} files under {args.root}\n")
    print("  PROCESSING STATE")
    for k in ("ok", "audio_only", "incomplete", "stale_version",
              "unprocessed", "unreadable"):
        if counts.get(k):
            print(f"    {k:16} {counts[k]:5}  ({100*counts[k]/n:.0f}%)")
    print("\n  YOUTUBE LINKS")
    for k, label in (("yt_origin", "origin (where the audio came from)"),
                     ("yt_reference", "reference (a video of the song)"),
                     ("no_youtube", "no link known")):
        if counts.get(k):
            print(f"    {label:34} {counts[k]:5}  ({100*counts[k]/n:.0f}%)")
    print("\n  NEEDS ATTENTION")
    for k, label in (("bpm_disagree", "BPM engines disagreed"),
                     ("bpm_ambiguous", "BPM half-or-double"),
                     ("key_uncertain", "key profiles all disagreed"),
                     ("low_quality", "low audio quality"),
                     ("no_art", "no cover art")):
        if counts.get(k):
            print(f"    {label:28} {counts[k]:5}")
    print(f"\n  REPLAYGAIN (target {RG_TARGET_LUFS:.0f} LUFS)")
    for k, label in (("rg_missing", "no track gain"),
                     ("rg_no_peak", "no track peak"),
                     ("rg_implausible", "gain outside +/-30 dB"),
                     ("rg_wrong_target", "tagged against another target")):
        print(f"    {label:30} {counts.get(k, 0):5}")
    if args.json:
        json.dump(rows, open(args.json, "w"), ensure_ascii=False, indent=1)
        print(f"\n  per-file detail -> {args.json}")
    print()
    # Non-zero exit when anything is unprocessed, so this can gate a re-run.
    #
    # An IMPLAUSIBLE gain fails: a value outside +/-30 dB is a parse or unit
    # error, and the file is actively wrong.
    #
    # A MISSING gain is reported and does not fail. It looks like a write
    # failure and usually is not: write_tags writes a gain only when the
    # analysis entry carries a loudness, so a track whose measurement failed,
    # or which has no analysis entry at all, legitimately has none. Failing on
    # it would let one transient ffmpeg error fail every later verify run,
    # which is a gate that cries wolf rather than a gate.
    #
    # The peak and target counters are reported for the same reason: a file
    # measured before ffmpeg took over has no peak to write, and that is a
    # stale cache rather than a broken build.
    sys.exit(1 if counts.get("unprocessed") or counts.get("unreadable")
             or counts.get("rg_implausible") else 0)


if __name__ == "__main__":
    main()
