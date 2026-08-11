#!/usr/bin/env python3
"""Collect Last.fm community tags for every artist and track, and cache them.

Last.fm is the only source that knows regional scenes. Deezer's taxonomy stops
at "Rap/Hip Hop" and MusicBrainz genres are sparse for this repertoire, but the
Last.fm community tags Balkan music by nationality ("croatian", "beograd"),
by scene ("ex-yu rock", "novi val", "serbian rap") and by era.

Two levels, and they answer different questions:

  * artist tags describe the act. Broad, well populated, and what you want for
    "everything by this scene".
  * track tags describe the song. Sparser, but a rapper's acoustic ballad is
    tagged differently from the rest of their catalogue.

Both are collected. Nothing is written into files here -- this is the raw
material, cached so that deciding what to do with it costs nothing to redo.

Tags are free text, so the same idea arrives many ways ("ex-yu rock",
"ex-yu-rock", "exyu rock"). Normalisation happens where they are used, not
here: throwing information away at collection time cannot be undone.

Usage:
  lastfm_tags.py                 # fill the cache
  lastfm_tags.py --report        # what was collected, by frequency
  lastfm_tags.py --report --origin hr,rs   # only artists from these countries
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import artist_names  # noqa: E402
from pipeline.write_tags import split_credits  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
ORIGIN = os.path.join(HERE, "cache", "artist_origin.json")
SECRETS = os.path.join(HERE, "config", "secrets.json")
OUT = os.path.join(HERE, "cache", "lastfm_tags.json")

RATE = 5.0
# Below this share of the top tag's weight, a tag is one or two people's
# opinion rather than a description anyone would recognise.
MIN_WEIGHT = 10


def _get(session, key, params):
    params = dict(params, api_key=key, format="json", autocorrect=1)
    for attempt in range(3):
        try:
            r = session.get("https://ws.audioscrobbler.com/2.0/",
                            params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return {}


def tags_for(session, key, method, params, cache, cache_key):
    if cache_key in cache:
        return cache[cache_key]
    data = _get(session, key, dict(params, method=method))
    raw = (data.get("toptags") or {}).get("tag") or []
    if isinstance(raw, dict):
        raw = [raw]
    out = [{"name": t["name"].lower(), "count": int(t.get("count") or 0)}
           for t in raw if t.get("name")]
    cache[cache_key] = out
    time.sleep(1.0 / RATE)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--origin", help="comma-separated country codes to report on")
    ap.add_argument("--tracks", action="store_true",
                    help="also fetch per-track tags (slower)")
    ap.add_argument("--min-weight", type=int, default=MIN_WEIGHT)
    args = ap.parse_args()

    rows = json.load(open(REVIEW))
    mapping, _w, _g = artist_names.build(rows)
    origin = json.load(open(ORIGIN)) if os.path.exists(ORIGIN) else {}
    cache = json.load(open(OUT)) if os.path.exists(OUT) else {"artist": {}, "track": {}}
    cache.setdefault("artist", {})
    cache.setdefault("track", {})

    key = json.load(open(SECRETS)).get("lastfm_api_key")
    session = requests.Session()

    # One entry per (artist, title) actually in the library.
    wanted = []
    for r in rows:
        a, t = r.get("proposed_artist"), r.get("proposed_title")
        if not (a and t):
            continue
        lead = artist_names.canonical(split_credits(a, t)[0], mapping)
        wanted.append((lead, t))
    artists = sorted({a for a, _ in wanted})

    if not args.report:
        print(f"\n  {len(artists)} artists"
              f"{f', {len(wanted)} tracks' if args.tracks else ''}\n")
        for i, a in enumerate(artists, 1):
            tags_for(session, key, "artist.gettoptags", {"artist": a},
                     cache["artist"], a)
            if i % 100 == 0:
                print(f"    {i}/{len(artists)} artists", flush=True)
                json.dump(cache, open(OUT, "w"), ensure_ascii=False, indent=1)
        if args.tracks:
            for i, (a, t) in enumerate(wanted, 1):
                tags_for(session, key, "track.gettoptags",
                         {"artist": a, "track": t}, cache["track"], f"{a}|{t}")
                if i % 200 == 0:
                    print(f"    {i}/{len(wanted)} tracks", flush=True)
                    json.dump(cache, open(OUT, "w"), ensure_ascii=False, indent=1)
        json.dump(cache, open(OUT, "w"), ensure_ascii=False, indent=1)
        print(f"\n  -> {OUT}\n")

    # ---- report ----
    want_cc = {c.strip() for c in (args.origin or "").split(",") if c.strip()}
    counts, per_tag_artists = Counter(), defaultdict(set)
    for a in artists:
        if want_cc:
            o = origin.get(a)
            if not o or o["country"] not in want_cc:
                continue
        for t in cache["artist"].get(a) or []:
            if t["count"] >= args.min_weight:
                counts[t["name"]] += 1
                per_tag_artists[t["name"]].add(a)
    scope = f"artists from {sorted(want_cc)}" if want_cc else "all artists"
    print(f"\n  {len(counts)} distinct tags across {scope} "
          f"(tag weight >= {args.min_weight})\n")
    for name, n in counts.most_common(60):
        who = ", ".join(sorted(per_tag_artists[name])[:3])
        print(f"    {n:4}  {name[:32]:32} {who[:52]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
