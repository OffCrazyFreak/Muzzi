#!/usr/bin/env python3
"""Stage 6: cover art and genre for identified tracks.

Art: beets' fetchart is album-oriented and returns nothing for singletons,
which is why the earlier run finished with 0% art. We hold release-group MBIDs
for most matches, so we go straight to the Cover Art Archive, then fall back to
iTunes and Deezer (both free, no key, no auth).

Genre: neither AcoustID nor the release-group data carries one, so it comes
from Last.fm's top tags, filtered against a stopword list because raw Last.fm
tags include things like "favourite" and "seen live".

Both are network-bound and cache to disk, so re-runs cost nothing.

Usage: enrich.py [--limit N] [-j workers]
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

REVIEW = os.path.join(HERE, "cache", "review.json")
SECRETS = os.path.join(HERE, "config", "secrets.json")
ARTDIR = os.path.join(HERE, "cache", "art")
OUT = os.path.join(HERE, "cache", "enrich.json")

from pipeline.useragent import UA  # noqa: E402
CAA = "https://coverartarchive.org/release-group/{}/front-500"
ITUNES = "https://itunes.apple.com/search"
DEEZER = "https://api.deezer.com/search"
LASTFM = "https://ws.audioscrobbler.com/2.0/"

# Last.fm tags are user-generated and full of non-genres.
TAG_STOP = {
    "favorites", "favourites", "favorite", "favourite", "seen live", "awesome",
    "loved", "love", "best", "good", "cool", "beautiful", "my music", "spotify",
    "albums i own", "check out", "under 2000 listeners", "songs", "music",
    "male vocalists", "female vocalists", "00s", "10s", "90s", "80s", "70s",
    "60s", "20s", "2010s", "2020s", "2000s", "1990s", "1980s",
}


# Source trust: Cover Art Archive art is keyed to the exact release MBID, so
# it is essentially always the right cover. iTunes and Deezer are matched by
# TEXT SEARCH and can return a different album entirely.
ART_TRUST = {"coverartarchive": "high", "itunes": "medium", "deezer": "medium"}


def art_sanity(data):
    """Cheap structural checks. Catches placeholders, broken fetches and
    single-colour images -- not 'is this the right album', which nothing cheap
    can answer."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        problems = []
        if min(w, h) < 250:
            problems.append(f"small ({w}x{h})")
        if h and abs(w / h - 1.0) > 0.12:
            problems.append(f"not square ({w}x{h})")
        # A cover with almost no colour variation is a placeholder or a fetch
        # that returned a blank/error image.
        sm = im.convert("RGB").resize((32, 32))
        px = list(sm.getdata())
        spread = max(max(c) - min(c) for c in zip(*px))
        if spread < 24:
            problems.append("near-flat colour (likely placeholder)")
        return {"width": w, "height": h, "problems": problems or None}
    except Exception as e:
        return {"problems": [f"undecodable: {type(e).__name__}"]}


def fetch_art(row, session):
    """Cover Art Archive by release-group, then iTunes, then Deezer."""
    rgid = row.get("release_group_id")
    artist, title = row.get("proposed_artist"), row.get("proposed_title")
    album = row.get("proposed_album")

    if rgid:
        try:
            r = session.get(CAA.format(rgid), headers=UA, timeout=30,
                            allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 5000:
                return r.content, "coverartarchive"
        except Exception:
            pass
    q = f"{artist} {album or title}".strip()
    if not q:
        return None, None
    try:
        r = session.get(ITUNES, params={"term": q, "entity": "song", "limit": 1},
                        timeout=30)
        res = (r.json().get("results") or [None])[0] if r.status_code == 200 else None
        if res and res.get("artworkUrl100"):
            # iTunes serves any size by substituting the dimensions in the URL.
            url = res["artworkUrl100"].replace("100x100", "600x600")
            img = session.get(url, timeout=30)
            if img.status_code == 200 and len(img.content) > 5000:
                return img.content, "itunes"
    except Exception:
        pass
    try:
        r = session.get(DEEZER, params={"q": q, "limit": 1}, timeout=30)
        res = (r.json().get("data") or [None])[0] if r.status_code == 200 else None
        cover = (res or {}).get("album", {}).get("cover_xl")
        if cover:
            img = session.get(cover, timeout=30)
            if img.status_code == 200 and len(img.content) > 5000:
                return img.content, "deezer"
    except Exception:
        pass
    return None, None


def fetch_genres(row, session, key):
    if not key:
        return []
    artist, title = row.get("proposed_artist"), row.get("proposed_title")
    if not (artist and title):
        return []
    # Track tags are more specific than artist tags; fall back to the artist.
    for method, params in (("track.gettoptags", {"artist": artist, "track": title}),
                           ("artist.gettoptags", {"artist": artist})):
        try:
            r = session.get(LASTFM, params={
                "method": method, "api_key": key, "format": "json",
                "autocorrect": 1, **params}, headers=UA, timeout=30)
            if r.status_code != 200:
                continue
            d = r.json()
            tags = (d.get("toptags") or {}).get("tag") or []
            if isinstance(tags, dict):
                tags = [tags]
            out = []
            for t in tags:
                name = (t.get("name") or "").strip()
                if not name or name.lower() in TAG_STOP:
                    continue
                # Ignore long-tail noise; count is a percentile 0-100.
                if int(t.get("count") or 0) < 10:
                    continue
                out.append(name.title())
                if len(out) == 3:
                    break
            if out:
                return out
        except Exception:
            continue
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("-j", "--workers", type=int,
                    default=max(4, min(8, (os.cpu_count() or 4))))
    ap.add_argument("--min-confidence", type=float, default=0.0)
    args = ap.parse_args()

    rows = [r for r in json.load(open(REVIEW))
            if r.get("proposed_artist") and r["confidence"] >= args.min_confidence]
    if args.limit:
        rows = rows[: args.limit]
    done = json.load(open(OUT)) if os.path.exists(OUT) else {}
    todo = [r for r in rows if r["path"] not in done]
    os.makedirs(ARTDIR, exist_ok=True)

    key = json.load(open(SECRETS)).get("lastfm_api_key")
    print(f"  {len(todo)} to enrich ({len(done)} cached), {args.workers} workers")
    if not todo:
        print("  nothing to do\n")
        return

    session = requests.Session()
    t0 = time.time()

    def work(r):
        rec = {"path": r["path"], "file": r["file"]}
        img, src = fetch_art(r, session)
        if img:
            # Name art by fingerprint-independent key: the output filename may
            # change, but the source path will not within a run.
            h = str(abs(hash(r["path"])))[:16]
            p = os.path.join(ARTDIR, f"{h}.jpg")
            with open(p, "wb") as fh:
                fh.write(img)
            rec["art_path"], rec["art_source"] = p, src
            rec["art_trust"] = ART_TRUST.get(src, "unknown")
            rec["art_check"] = art_sanity(img)
        rec["genres"] = fetch_genres(r, session, key)
        return rec

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, r): r for r in todo}
        for i, f in enumerate(as_completed(futs), 1):
            rec = f.result()
            done[rec["path"]] = rec
            if i % 20 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"  {i}/{len(todo)}  {el:.0f}s  {el/i:.2f}s/track "
                      f"eta {(len(todo)-i)*el/i:.0f}s", flush=True)
                json.dump(done, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
                os.replace(OUT + ".tmp", OUT)

    json.dump(done, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)

    vals = list(done.values())
    art = [v for v in vals if v.get("art_path")]
    gen = [v for v in vals if v.get("genres")]
    from collections import Counter
    print(f"\n  art  {len(art)}/{len(vals)} ({100*len(art)/max(len(vals),1):.0f}%)"
          f"  sources: {dict(Counter(v['art_source'] for v in art))}")
    print(f"  genre {len(gen)}/{len(vals)} ({100*len(gen)/max(len(vals),1):.0f}%)")
    bad = [v for v in art if (v.get("art_check") or {}).get("problems")]
    low = [v for v in art if v.get("art_trust") != "high"]
    print(f"  art failing sanity checks: {len(bad)}")
    print(f"  art matched by text search rather than release ID "
          f"(less trustworthy): {len(low)}")
    for v in bad[:6]:
        print(f"      {v['file'][:46]:46} "
              f"{'; '.join((v['art_check'] or {}).get('problems') or [])}")
    print(f"  wall {time.time()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
