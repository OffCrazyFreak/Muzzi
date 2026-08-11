#!/usr/bin/env python3
"""Second-pass enrichment: use what the earlier stages learned as a new key.

Identification produces an album for tracks that had nothing before, and an
album plus an artist is a far better search key than the filename ever was.
The catalogues that confirmed those names do not return a year from their
search endpoints, so 129 tracks currently sit with an album and no year --
recoverable, because the album can now be looked up directly.

Sources, in descending order of how much they can be trusted about a date:

  MusicBrainz  release-group first-release-date. This is the ORIGINAL release
               year of the work, which is what belongs in a year tag; a
               reissue or a compilation appearance is not the song's year.
  Deezer       /album/{id} release_date, for everything MusicBrainz lacks.
  iTunes       collection releaseDate, last because it dates the store
               listing and skews late for older catalogue.

Only tracks that already have an album and are missing a year are touched, so
re-running is cheap and nothing that already has a good year gets overwritten.

Usage:
  enrich_release.py               # every auto-accepted track missing a year
  enrich_release.py --limit 20
  enrich_release.py --include-review
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.identify import RateLimiter  # noqa: E402
from pipeline.webmatch import fit  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
OUT = os.path.join(HERE, "cache", "release_years.json")
from pipeline.useragent import UA  # noqa: E402

MB_RATE = 0.85          # documented limit is 1 req/s per IP
DEEZER_RATE = 8.0
ITUNES_RATE = 0.33

MB_RG = "https://musicbrainz.org/ws/2/release-group"
_YEAR = re.compile(r"(\d{4})")


def _year(s):
    m = _YEAR.search(str(s or ""))
    if not m:
        return None
    y = int(m.group(1))
    # Recorded music predates neither 1900 nor, obviously, next year.
    return y if 1900 <= y <= 2030 else None


def mb_escape(s):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s or "")


def from_musicbrainz(artist, album, lim, session):
    lim.wait()
    q = f'releasegroup:"{mb_escape(album)}" AND artist:"{mb_escape(artist)}"'
    try:
        r = session.get(MB_RG, params={"query": q, "fmt": "json", "limit": 5},
                        headers=UA, timeout=25)
        if r.status_code != 200:
            return None
        groups = r.json().get("release-groups") or []
    except Exception:
        return None
    for g in groups:
        arts = "; ".join(a["artist"]["name"] for a in g.get("artist-credit", [])
                         if isinstance(a, dict) and a.get("artist"))
        if fit(artist, arts) < 0.6 or fit(album, g.get("title")) < 0.6:
            continue
        y = _year(g.get("first-release-date"))
        if y:
            return {"year": y, "source": "musicbrainz",
                    "release_group_id": g.get("id"),
                    "type": g.get("primary-type")}
    return None


def from_deezer(artist, album, lim, session):
    lim.wait()
    try:
        r = session.get("https://api.deezer.com/search/album",
                        params={"q": f'artist:"{artist}" album:"{album}"',
                                "limit": 5}, timeout=20)
        albums = r.json().get("data") or []
    except Exception:
        return None
    for a in albums:
        if fit(album, a.get("title")) < 0.6:
            continue
        lim.wait()
        try:
            d = session.get(f"https://api.deezer.com/album/{a['id']}",
                            timeout=20).json()
        except Exception:
            continue
        y = _year(d.get("release_date"))
        if y:
            return {"year": y, "source": "deezer",
                    "cover": d.get("cover_xl"),
                    "type": d.get("record_type")}
    return None


def from_itunes(artist, album, lim, session):
    lim.wait()
    try:
        r = session.get("https://itunes.apple.com/search",
                        params={"term": f"{artist} {album}", "media": "music",
                                "entity": "album", "limit": 5}, timeout=20)
        res = r.json().get("results") or []
    except Exception:
        return None
    for a in res:
        if fit(album, a.get("collectionName")) < 0.6:
            continue
        y = _year(a.get("releaseDate"))
        if y:
            return {"year": y, "source": "itunes",
                    "cover": (a.get("artworkUrl100") or "").replace("100x100", "600x600") or None}
    return None


def lookup(artist, album, ctx):
    for fn, key in ((from_musicbrainz, "mb"), (from_deezer, "deezer"),
                    (from_itunes, "itunes")):
        got = fn(artist, album, ctx["lims"][key], ctx["session"])
        if got:
            return got
    return None


def targets(rows, include_review):
    tiers = {"auto", "review"} if include_review else {"auto"}
    out = []
    for r in rows:
        if r["tier"] not in tiers or r.get("proposed_year"):
            continue
        if not (r.get("proposed_artist") and r.get("proposed_album")):
            continue
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--include-review", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int)
    args = ap.parse_args()

    rows = json.load(open(REVIEW))
    cache = {} if args.force else (json.load(open(OUT)) if os.path.exists(OUT) else {})
    todo = [r for r in targets(rows, args.include_review) if r["path"] not in cache]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"  nothing missing a year ({len(cache)} cached)\n")
        return 0

    # MusicBrainz allows one request a second, so more workers than that only
    # buys parallelism on the two fallbacks.
    workers = args.workers or min(6, (os.cpu_count() or 4))
    ctx = {"lims": {"mb": RateLimiter(MB_RATE), "deezer": RateLimiter(DEEZER_RATE),
                    "itunes": RateLimiter(ITUNES_RATE)},
           "session": requests.Session()}

    print(f"  {len(todo)} tracks have an album but no year, {workers} workers\n")
    lock, found, t0 = threading.Lock(), {"n": 0, "hit": 0}, time.time()

    def work(r):
        return r["path"], r, lookup(r["proposed_artist"], r["proposed_album"], ctx)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, r) for r in todo]
        for f in as_completed(futs):
            try:
                path, r, got = f.result()
            except Exception as e:
                print(f"    worker failed: {str(e)[:90]}", flush=True)
                continue
            with lock:
                found["n"] += 1
                cache[path] = got or {"year": None, "source": None}
                if got:
                    found["hit"] += 1
                    print(f"    [{found['n']}/{len(todo)}] {got['year']} "
                          f"{got['source']:12} {r['proposed_artist'][:22]:22} | "
                          f"{str(r['proposed_album'])[:30]}", flush=True)
                if found["n"] % 25 == 0:
                    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
                    os.replace(OUT + ".tmp", OUT)

    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    n = max(found["n"], 1)
    print(f"\n  {found['hit']}/{found['n']} years recovered "
          f"({100*found['hit']/n:.0f}%) in {time.time()-t0:.0f}s")
    print(f"  -> {OUT}\n  now re-run review.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
