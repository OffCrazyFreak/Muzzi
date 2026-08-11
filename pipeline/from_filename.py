#!/usr/bin/env python3
"""Last resort for tracks nothing could identify: believe the filename.

161 tracks survive fingerprinting AND text search unmatched, and almost all of
them are already named "Artist - Title (ft. Other)". That name was typed by a
human who knew what the song was, which makes it better evidence than anything
the databases failed to provide.

Two steps, in order of how much they can be trusted:

  1. Parse the filename into artist / title / featured artists.
  2. Ask MusicBrainz whether that artist really recorded that title. A hit
     upgrades the guess to a real identity with an album, year and MBID.

A miss is not a failure. Balkan music is thinly covered by MusicBrainz -- that
is why these tracks are unmatched in the first place -- so a filename that no
database confirms is still the best name available. Those are kept separate
and marked `filename-only`, so the difference between "confirmed" and
"believed" never gets lost.

Usage:
  from_filename.py                 # guess + verify every unmatched track
  from_filename.py --limit 20
  from_filename.py --no-lookup     # parse only, skip MusicBrainz
"""
import argparse
import json
import os
import re
import sys

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.probe_match import split_name  # noqa: E402
from pipeline.textsearch import (MB_RATE, MB_URL, RateLimiter,  # noqa: E402
                                 best_release, get_with_retry, mb_escape)
from pipeline.write_tags import split_credits  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
OUT = os.path.join(HERE, "cache", "filename_guess.json")
from pipeline.useragent import UA  # noqa: E402

# A leading "02. " or "12 - " is a track number, never part of the artist.
_TRACKNO = re.compile(r"^\s*\d{1,2}\s*[.)-]\s+")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def guess(filename):
    """-> (artist, title, [featured]) or (None, None, []) if unsplittable."""
    stem = _TRACKNO.sub("", os.path.splitext(filename)[0])
    artist, title = split_name(stem)
    if not (artist and title):
        return None, None, []
    lead, feats, plain_title = split_credits(artist, title)
    return lead, plain_title, feats


def confirm(artist, title, limiter, session):
    """Ask MusicBrainz whether this artist recorded this title."""
    if not (artist and title):
        return None
    q = f'recording:"{mb_escape(title)}" AND artist:"{mb_escape(artist)}"'
    try:
        r = get_with_retry(session, MB_URL, limiter,
                           params={"query": q, "fmt": "json", "limit": 5},
                           headers=UA)
        if r.status_code != 200:
            return None
        recs = r.json().get("recordings") or []
    except Exception:
        return None

    want_a, want_t = _norm(artist), _norm(title)
    for rec in recs:
        arts = [a["artist"]["name"] for a in rec.get("artist-credit", [])
                if isinstance(a, dict) and a.get("artist")]
        got_a = _norm("; ".join(arts))
        got_t = _norm(rec.get("title"))
        # Require BOTH names to line up. MusicBrainz will happily return a
        # different song by the right artist, and accepting that would be
        # worse than keeping the filename.
        a_ok = want_a and (want_a in got_a or got_a in want_a)
        t_ok = want_t and (want_t in got_t or got_t in want_t)
        if a_ok and t_ok:
            return {"recording_id": rec.get("id"),
                    "artist": "; ".join(arts) if arts else artist,
                    "title": rec.get("title") or title,
                    "release": best_release(rec) or None,
                    "mb_score": rec.get("score")}
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-lookup", action="store_true")
    args = ap.parse_args()

    rows = [r for r in json.load(open(REVIEW)) if r["tier"] == "unmatched"]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("  nothing unmatched\n")
        return 0

    cache = json.load(open(OUT)) if os.path.exists(OUT) else {}
    limiter, session = RateLimiter(MB_RATE), requests.Session()
    confirmed = believed = unsplittable = 0

    print(f"  {len(rows)} unmatched tracks to guess from their filenames")
    for i, r in enumerate(rows, 1):
        artist, title, feats = guess(r["file"])
        if not artist:
            unsplittable += 1
            cache[r["path"]] = {"file": r["file"], "status": "unsplittable"}
            continue
        rec = {"file": r["file"], "artist": artist, "title": title,
               "featured": feats, "status": "filename-only"}
        if not args.no_lookup:
            hit = confirm(artist, title, limiter, session)
            if hit:
                rec.update({"artist": hit["artist"], "title": hit["title"],
                            "recording_id": hit["recording_id"],
                            "release": hit["release"],
                            "status": "musicbrainz-confirmed"})
        cache[r["path"]] = rec
        confirmed += rec["status"] == "musicbrainz-confirmed"
        believed += rec["status"] == "filename-only"
        if i % 20 == 0 or i == len(rows):
            print(f"    {i}/{len(rows)}  confirmed {confirmed}, "
                  f"filename-only {believed}", flush=True)
            json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
            os.replace(OUT + ".tmp", OUT)

    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    print(f"\n  MusicBrainz confirmed the filename : {confirmed}")
    print(f"  filename believed, unconfirmed     : {believed}")
    print(f"  filename could not be split        : {unsplittable}")
    print(f"  -> {OUT}\n  now re-run review.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
