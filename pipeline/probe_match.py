#!/usr/bin/env python3
"""Calibration probe: what match distances do we actually get?

Replicates what beets does during a real singleton import -- seed the item's
artist/title from the filename (as the fromfilename plugin does), then ask the
autotagger for candidates -- and prints the distance of each. The point is to
choose `strong_rec_thresh` from evidence instead of copying a number off a blog.

Run against a stratified sample; MusicBrainz allows ~1 req/s so keep it small.

Usage: probe_match.py <music_dir> [-n 20] [--seed 7]
"""
import argparse
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.fingerprint import likely_balkan  # noqa: E402

from beets import config, library, plugins  # noqa: E402
from beets.autotag import match as bmatch  # noqa: E402

# Same cruft rules as survey.py: YouTube decorations plus BCMS equivalents
# ("tekst" = lyrics, "spot" = music video, "uzivo" = live).
CRUFT = re.compile(
    r"""\s*(?:
        [\(\[]\s*(?:official\s*)?(?:music\s*)?(?:lyrics?|lyric|video|audio|visualizer|
            hd|hq|4k|full\s*hd|mv|clip|version|remaster(?:ed)?(?:\s*\d{4})?)\s*[^\)\]]*[\)\]]
      | [\(\[]\s*(?:official|explicit|clean|uncensored)\s*[^\)\]]*[\)\]]
      | \b(?:official\s+video|official\s+audio|lyrics?\s+video|music\s+video)\b
      | \b(?:tekst|uzivo|u[zž]ivo|spot|prevod)\b
      | \b(?:hd|hq|4k)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)
# yt-dlp substitutes a fullwidth colon for ":" in filenames, so
# "twenty one pilots： Heathens" is an artist/title separator too.
SPLIT = re.compile(r"\s+[-–—]\s+|\s+[-–—]|[-–—]\s+|\s*：\s*|\s+:\s+")


def clean(stem):
    prev, out = None, stem
    while out != prev:
        prev, out = out, CRUFT.sub(" ", out)
    return re.sub(r"\s{2,}", " ", out).strip(" -–—_")


def split_name(stem):
    parts = [p.strip() for p in SPLIT.split(clean(stem)) if p.strip()]
    if len(parts) >= 2:
        return parts[0], " - ".join(parts[1:])
    return None, clean(stem)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    config.read()
    plugins.load_plugins()
    plugins.send("pluginload")

    files = sorted(
        f for f in os.listdir(args.root) if f.lower().endswith(".mp3")
    )
    # Stratify so the sample reflects the real Balkan/other split rather than
    # whatever alphabetical order happens to give us.
    bal = [f for f in files if likely_balkan(f)]
    oth = [f for f in files if not likely_balkan(f)]
    random.seed(args.seed)
    half = max(args.n // 2, 1)
    sample = random.sample(bal, min(half, len(bal))) + \
             random.sample(oth, min(args.n - half, len(oth)))

    rows = []
    for i, fn in enumerate(sample, 1):
        stem = os.path.splitext(fn)[0]
        artist, title = split_name(stem)
        item = library.Item.from_path(os.path.join(args.root, fn))
        if artist:
            item.artist = artist
        item.title = title

        try:
            prop = bmatch.tag_item(item)
            cands = list(prop.candidates)
        except Exception as e:
            print(f"  [{i}/{len(sample)}] {fn[:50]} ERROR {str(e)[:60]}")
            continue

        top = cands[0] if cands else None
        row = {
            "file": fn,
            "balkan": likely_balkan(fn),
            "parsed_artist": artist,
            "parsed_title": title,
            "n_candidates": len(cands),
            "best_distance": round(float(top.distance.distance), 3) if top else None,
            "best": f"{top.info.artist} - {top.info.title}" if top else None,
            "source": top.info.data_source if top else None,
            "recommendation": str(prop.recommendation),
        }
        rows.append(row)
        flag = "BAL" if row["balkan"] else "   "
        d = row["best_distance"]
        print(f"  [{i:2}/{len(sample)}] {flag} d={d if d is not None else '   -':>5} "
              f"{fn[:38]:38} -> {str(row['best'])[:44]}", flush=True)

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cache", "match_probe.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)

    # ---- calibration summary ----
    scored = [r for r in rows if r["best_distance"] is not None]
    print(f"\n  {len(scored)}/{len(rows)} got at least one candidate")
    for label, subset in (("Balkan", [r for r in scored if r["balkan"]]),
                          ("Other", [r for r in scored if not r["balkan"]])):
        if not subset:
            continue
        ds = sorted(r["best_distance"] for r in subset)
        mid = ds[len(ds) // 2]
        print(f"    {label:7} n={len(ds):3}  min={ds[0]:.3f}  median={mid:.3f}  max={ds[-1]:.3f}")
    print("\n  tracks accepted at each candidate threshold:")
    for t in (0.04, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
        b = sum(1 for r in scored if r["balkan"] and r["best_distance"] <= t)
        o = sum(1 for r in scored if not r["balkan"] and r["best_distance"] <= t)
        print(f"    <= {t:.2f}   other {o:3}   balkan {b:3}")
    print(f"\n  detail -> {out}\n")


if __name__ == "__main__":
    main()
