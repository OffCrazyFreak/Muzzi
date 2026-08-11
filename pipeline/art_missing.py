#!/usr/bin/env python3
"""Cover art for the tracks the cascade could not find an album for.

What is left after cascade.py is the awkward residue: bootleg remixes, covers,
regional uploads and anything else with no release behind it. They have no
album, so there is no album cover to fetch -- but they are not artless. A
YouTube upload has a thumbnail, and that image IS the cover as far as anyone
who knows the track is concerned.

Sources, best first:

  1. Deezer / iTunes track search, exact version match required. A remix must
     not silently inherit the original's sleeve.
  2. YouTube Music song thumbnail. For a track that exists nowhere else, this
     is the real artwork.
  3. Deezer artist picture, recorded as `artist` quality so it is obvious in
     the report that the image is of a person, not a release.

Writes cache/art_extra.json, which write_tags.py reads after the cascade's own
artwork. Nothing here overwrites an existing cover.

Usage:
  art_missing.py
  art_missing.py --limit 20 --batch 50
  art_missing.py --no-artist-fallback
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.identify import RateLimiter  # noqa: E402
from pipeline.webmatch import fit, version_mismatch  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
CASCADE = os.path.join(HERE, "cache", "cascade.json")
ART_DIR = os.path.join(HERE, "cache", "art")
OUT = os.path.join(HERE, "cache", "art_extra.json")

MIN_BYTES = 3000
# YouTube answers a missing maxresdefault with a small grey placeholder
# rather than a 404, so size is the only way to spot it.
MIN_EDGE = 400
RATE_DEEZER = 8.0
RATE_ITUNES = 0.33
RATE_YTM = 6.0

_YTM = None
_YTM_LOCK = threading.Lock()


def _ytmusic():
    global _YTM
    with _YTM_LOCK:
        if _YTM is None:
            from ytmusicapi import YTMusic
            _YTM = YTMusic()
        return _YTM


def from_deezer(artist, title, lim, session):
    lim.wait()
    try:
        data = session.get("https://api.deezer.com/search",
                           params={"q": f'artist:"{artist}" track:"{title}"',
                                   "limit": 5}, timeout=20).json().get("data") or []
    except Exception:
        return None
    for t in data:
        if fit(artist, (t.get("artist") or {}).get("name")) < 0.6:
            continue
        if fit(title, t.get("title")) < 0.6 or version_mismatch(title, t.get("title")):
            continue
        url = (t.get("album") or {}).get("cover_xl")
        if url:
            return {"url": url, "source": "deezer", "quality": "album"}
    return None


def from_itunes(artist, title, lim, session):
    lim.wait()
    try:
        res = session.get("https://itunes.apple.com/search",
                          params={"term": f"{artist} {title}", "media": "music",
                                  "entity": "song", "limit": 5},
                          timeout=20).json().get("results") or []
    except Exception:
        return None
    for t in res:
        if fit(artist, t.get("artistName")) < 0.6:
            continue
        if fit(title, t.get("trackName")) < 0.6 or version_mismatch(title, t.get("trackName")):
            continue
        url = (t.get("artworkUrl100") or "").replace("100x100", "600x600")
        if url:
            return {"url": url, "source": "itunes", "quality": "album"}
    return None


def from_ytmusic(artist, title, lim):
    """Songs first, then videos.

    A bootleg remix is not in the songs index -- no distributor delivered it --
    but it is very much on YouTube as a video, and that video's thumbnail is
    the artwork anyone who knows the track would recognise. Far better than
    falling through to a photograph of the artist."""
    for kind in ("songs", "videos"):
        lim.wait()
        try:
            res = _ytmusic().search(f"{artist} {title}", filter=kind, limit=5)
        except Exception:
            continue
        for x in res:
            names = ", ".join(a["name"] for a in x.get("artists") or [])
            # A video's "artist" is often the uploading channel, so for videos
            # the title has to carry the weight on its own.
            if kind == "songs" and fit(artist, names) < 0.5:
                continue
            if fit(title, x.get("title")) < 0.6:
                continue
            # The API's own thumbnail is 400x225, which centre-crops to
            # 225x225 -- visibly blurry on a phone. YouTube serves the full
            # frame by video id, so ask for that and fall back down the
            # resolution ladder.
            urls = []
            vid = x.get("videoId")
            if vid:
                urls += [f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
                         f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
                         f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"]
            urls += [t["url"] for t in (x.get("thumbnails") or [])][::-1]
            if urls:
                return {"urls": urls, "url": urls[0],
                        "source": f"ytmusic {kind}", "quality": "thumbnail"}
    return None


def from_deezer_artist(artist, lim, session):
    lim.wait()
    try:
        data = session.get("https://api.deezer.com/search/artist",
                           params={"q": artist, "limit": 3},
                           timeout=20).json().get("data") or []
    except Exception:
        return None
    for a in data:
        if fit(artist, a.get("name")) >= 0.8 and a.get("picture_xl"):
            return {"url": a["picture_xl"], "source": "deezer",
                    "quality": "artist"}
    return None


# Cover art is expected to be square: players crop or stretch anything else,
# and a YouTube video thumbnail is 16:9. 600px squared, JPEG, is the middle of
# the range everything recommends -- big enough not to blur on a phone screen,
# small enough that a few hundred covers do not add a gigabyte to the library.
ART_EDGE = 600


def _squarify(data):
    """-> JPEG bytes, centre-cropped to 1:1. Returns the original on failure,
    since a wrongly-shaped cover still beats no cover."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        w, h = im.size
        if w != h:
            edge = min(w, h)
            im = im.crop(((w - edge) // 2, (h - edge) // 2,
                          (w - edge) // 2 + edge, (h - edge) // 2 + edge))
        if min(im.size) < MIN_EDGE:
            raise ValueError("too small to use as a cover")
        if im.size[0] > ART_EDGE:
            im = im.resize((ART_EDGE, ART_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue(), (w != h)
    except ValueError:
        raise
    except Exception:
        return data, False


def download(url, session):
    """-> (path, was_cropped) or (None, False)."""
    dst = os.path.join(ART_DIR,
                       hashlib.sha1(("sq:" + url).encode()).hexdigest()[:20] + ".jpg")
    if os.path.exists(dst) and os.path.getsize(dst) >= MIN_BYTES:
        return dst, False
    try:
        r = session.get(url, timeout=30)
    except Exception:
        return None, False
    if r.status_code != 200 or len(r.content) < MIN_BYTES:
        return None, False
    try:
        data, cropped = _squarify(r.content)
    except ValueError:
        return None, False
    tmp = dst + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dst)
    return dst, cropped


def find(row, ctx, allow_artist):
    artist, title = row.get("proposed_artist"), row.get("proposed_title")
    if not (artist and title):
        return {"status": "no name"}
    lims, session = ctx["lims"], ctx["session"]
    for fn in (lambda: from_deezer(artist, title, lims["deezer"], session),
               lambda: from_itunes(artist, title, lims["itunes"], session),
               lambda: from_ytmusic(artist, title, lims["ytm"])):
        got = fn()
        if got:
            for url in got.get("urls") or [got["url"]]:
                path, cropped = download(url, session)
                if path:
                    return {"status": "found", "path": path,
                            "cropped_to_square": cropped,
                            **{k: v for k, v in got.items() if k != "urls"}}
    if allow_artist:
        got = from_deezer_artist(artist, lims["deezer"], session)
        if got:
            path, cropped = download(got["url"], session)
            if path:
                return {"status": "found", "path": path,
                        "cropped_to_square": cropped, **got}
    return {"status": "nothing found"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--no-artist-fallback", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    os.makedirs(ART_DIR, exist_ok=True)
    rows = json.load(open(REVIEW))
    cascade = json.load(open(CASCADE)) if os.path.exists(CASCADE) else {}
    done = {} if args.force else (json.load(open(OUT)) if os.path.exists(OUT) else {})

    todo = []
    for r in rows:
        if r["tier"] not in ("auto", "hinted", "review"):
            continue
        if (cascade.get(r["path"]) or {}).get("facts", {}).get("cover_url"):
            continue
        if r["path"] in done:
            continue
        if r.get("proposed_artist") and r.get("proposed_title"):
            todo.append(r)
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"  every identified track already has artwork ({len(done)} extra)\n")
        return 0

    workers = args.workers or min(10, (os.cpu_count() or 4) * 2)
    ctx = {"lims": {"deezer": RateLimiter(RATE_DEEZER),
                    "itunes": RateLimiter(RATE_ITUNES),
                    "ytm": RateLimiter(RATE_YTM)},
           "session": requests.Session()}

    print(f"\n  {len(todo)} identified tracks have no artwork, "
          f"{workers} workers, batches of {args.batch}\n")
    lock, stats, t0 = threading.Lock(), {}, time.time()

    for start in range(0, len(todo), args.batch):
        batch = todo[start:start + args.batch]
        bn, total_b = start // args.batch + 1, (len(todo) + args.batch - 1) // args.batch
        print(f"  --- batch {bn}/{total_b}", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(find, r, ctx, not args.no_artist_fallback): r
                    for r in batch}
            for f in as_completed(futs):
                r = futs[f]
                try:
                    res = f.result()
                except Exception as e:
                    res = {"status": "nothing found", "why": str(e)[:60]}
                with lock:
                    done[r["path"]] = res
                    key = res["status"] if res["status"] != "found" \
                        else f"found ({res['source']}, {res['quality']})"
                    stats[key] = stats.get(key, 0) + 1
        json.dump(done, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
        os.replace(OUT + ".tmp", OUT)
        got = sum(v for k, v in stats.items() if k.startswith("found"))
        print(f"      {got} found so far", flush=True)

    print(f"\n  {len(todo)} tracks in {(time.time()-t0)/60:.1f} min\n")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {k:34} {v}")
    print(f"\n  -> {OUT}\n  re-run write_tags.py to embed them\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
