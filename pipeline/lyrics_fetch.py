#!/usr/bin/env python3
"""LRCLIB lookup that distinguishes "no lyrics exist" from "the request failed".

The original version did neither of two necessary things: it had no rate
limiting, and it cached an empty result on ANY failure -

    except Exception:
        pass
    cache[key] = out          # frozen as "no lyrics" forever

Measured consequence on this library: of 132 tracks recorded as having no
lyrics, 62 had SYNCED lyrics available on a clean re-query, including Avicii's
"The Nights" and Avril Lavigne's "Sk8er Boi". A 48% false-negative rate, and
because the miss was cached, no amount of re-running would ever fix it.

So: retry with backoff, and only ever write a negative result to the cache when
LRCLIB actually answered and said there was nothing. An error leaves the cache
untouched, so the next run retries it.

Query strategy follows Namida's, which is more thorough than one lookup:
several artist/title/album permutations plus a title cleaned of YouTube cruft.
Candidate selection is ours: LRCLIB returns many near-duplicate entries per
song, so we pick by closest duration to the actual file, which is what stops a
lyric sheet written for a different edit from drifting out of sync.
"""
import os
import re
import sys
import threading
import time

# LRCLIB is a free, donation-funded community service with no published limit.
# Two per second is brisk enough to finish 3000 tracks in ~25 minutes and light
# enough not to be rude.
RATE = 2.0
MAX_RETRIES = 4
RETRY_STATUS = {429, 500, 502, 503, 504}

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.webmatch import fit  # noqa: E402

BASE = "https://lrclib.net/api"
from pipeline.useragent import UA  # noqa: E402

_lock = threading.Lock()
_last = [0.0]


def _throttle():
    with _lock:
        wait = _last[0] + 1.0 / RATE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


# YouTube rips carry a lot of noise in the title that LRCLIB will never match.
_CRUFT = re.compile(
    r"""\s*[\(\[\|]?\s*
        (?:official\s*(?:music\s*)?(?:video|audio|lyric\s*video|4k|hd)
          |lyrics?\s*video|lyric\s*video|lyrics?
          |official|audio|video|hq|hd|4k|full\s*hd
          |visualizer|mv|m/?v
          |free\s*download|out\s*now
          |\d{4}\.?)
        \s*[\)\]\|]?\s*""",
    re.I | re.X)
_YEAR_TAIL = re.compile(r"\s*[\(\[]?\s*(?:19|20)\d{2}\.?\s*[\)\]]?\s*$")
_MULTISPACE = re.compile(r"\s{2,}")


def clean_title(title):
    if not title:
        return title
    out = _CRUFT.sub(" ", title)
    out = _YEAR_TAIL.sub("", out)
    out = _MULTISPACE.sub(" ", out).strip(" -_|.")
    return out or title


_ARTIST_SPLIT = re.compile(r"\s*(?:;|,|&|\bfeat\.?\b|\bft\.?\b|\bx\b|\bvs\.?\b)\s*",
                           re.I)


# How a foreign word gets written in Serbian and Croatian. These are not
# typos, they are the standard transliterations: "Kawasaki" is spelled
# "Kavasaki", "Michael" as "Majkl". LRCLIB holds whichever spelling the
# uploader used, so an exact title search misses the song entirely.
_TRANSLIT = [("w", "v"), ("y", "j"), ("x", "ks"), ("qu", "kv"), ("ck", "k"),
             ("ph", "f"), ("th", "t")]


def title_variants(title):
    """-> [title, ...] alternative spellings worth asking for."""
    if not title:
        return []
    out, low = [title], title.lower()
    for a, b in _TRANSLIT:
        if a in low:
            # Preserve the original case pattern well enough to read; LRCLIB
            # matches case-insensitively anyway.
            out.append(re.sub(a, b, title, flags=re.I))
    seen, uniq = set(), []
    for t in out:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq


def artist_variants(artist):
    """Full credit first, then the lead artist alone."""
    if not artist:
        return []
    out = [artist]
    parts = [p for p in _ARTIST_SPLIT.split(artist) if p.strip()]
    if parts and parts[0].strip() != artist.strip():
        out.append(parts[0].strip())
    return out


def _get(session, path, params):
    """-> ("ok", payload) | ("absent", None) | ("error", None).

    'absent' means LRCLIB answered and had nothing. Only that is safe to cache.
    """
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            r = session.get(f"{BASE}/{path}", params=params, headers=UA,
                            timeout=25)
        except Exception:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return "absent", None
        if r.status_code == 200:
            try:
                return "ok", r.json()
            except Exception:
                return "error", None
        if r.status_code in RETRY_STATUS:
            delay = r.headers.get("Retry-After")
            try:
                delay = float(delay)
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(min(delay, 30))
            continue
        return "error", None
    return "error", None


def _hits_from(payload):
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    return [h for h in payload if isinstance(h, dict)]


def _pick(hits, duration, artist=None):
    """Prefer the right artist, then synced, then the closest duration.

    LRCLIB routinely holds several entries for one song (7 for Rasta's
    "Euforija"). Taking the first is arbitrary; taking the nearest duration
    picks the edit we actually have, which is what keeps the timestamps aligned.

    Artist has to come first, though. Ranking on duration alone fetched
    JoelB's "MAMBA" for Grse's, and a Polish "Kawasaki" for Rasta's -- whole
    songs in the wrong language, embedded as if they were right, and then used
    to decide the track's language too.
    """
    if not hits:
        return None

    def score(h):
        d = h.get("duration")
        off = abs(d - duration) if (d and duration) else 999
        wrong_artist = 0
        if artist and h.get("artistName"):
            wrong_artist = 0 if fit(artist, h["artistName"]) >= 0.5 else 1
        return (wrong_artist, 0 if h.get("syncedLyrics") else 1, off)
    return sorted(hits, key=score)[0]


def fetch(artist, title, cache, session, album=None, duration=None):
    """-> {"synced": str|None, "plain": str|None, "status": "ok"|"absent"}.

    A cached entry is reused only when it is definitive. Legacy entries (which
    carry no 'status') that recorded nothing are retried, which is what repairs
    the 62 false negatives already sitting in the cache.
    """
    key = f"{artist}|{title}".lower()
    cached = cache.get(key)
    if isinstance(cached, dict):
        if cached.get("status") in ("ok", "absent"):
            return cached
        if cached.get("synced") or cached.get("plain"):
            cached.setdefault("status", "ok")       # legacy hit, still good
            return cached
        # legacy miss with no status: fall through and retry it
    elif isinstance(cached, str):
        cache[key] = {"plain": cached, "synced": None, "status": "ok"}
        return cache[key]

    ct = clean_title(title)
    artists = artist_variants(artist)

    attempts = []
    if duration and artists:
        # /api/get is the signed-match endpoint; try it before searching.
        attempts.append(("get", {"artist_name": artists[0], "track_name": title,
                                 "duration": int(duration)}))
        if album:
            attempts[-1][1]["album_name"] = album
    for a in artists:
        attempts.append(("search", {"track_name": title, "artist_name": a}))
        if ct != title:
            attempts.append(("search", {"track_name": ct, "artist_name": a}))
    if artists:
        attempts.append(("search", {"q": f"{artists[0]} {ct}"}))
    attempts.append(("search", {"q": ct}))
    # Last resort: the same title spelled the way a Balkan uploader would.
    # "Rasta - Kawasaki" is filed under "Kavasaki", so every attempt above
    # returns nothing at all while the lyrics sit there under one letter.
    for alt in title_variants(ct)[1:]:
        for a in artists[:1]:
            attempts.append(("search", {"q": f"{a} {alt}"}))
        attempts.append(("search", {"q": alt}))

    saw_answer = False
    best = None
    for path, params in attempts:
        status, payload = _get(session, path, params)
        if status == "error":
            continue                      # do NOT let a failure imply absence
        saw_answer = True
        if status == "absent":
            continue
        hit = _pick(_hits_from(payload), duration, artist)
        if not (hit and (hit.get("syncedLyrics") or hit.get("plainLyrics"))):
            continue
        right_artist = not artist or not hit.get("artistName") or \
            fit(artist, hit["artistName"]) >= 0.5
        # Stopping at the first attempt that returns anything is how a Polish
        # "Kawasaki" won: it was found by an earlier query than the Serbian
        # "Kavasaki" spelling that is actually this song. Keep the wrong-artist
        # hit only as a fallback and carry on looking for the right one.
        if right_artist:
            best = hit
            if hit.get("syncedLyrics"):
                break                     # synced by the right artist: done
        elif best is None:
            best = hit

    if best:
        out = {"synced": best.get("syncedLyrics") or None,
               "plain": best.get("plainLyrics") or None,
               "status": "ok",
               "matched": f'{best.get("artistName")} - {best.get("trackName")}',
               "matched_duration": best.get("duration")}
        cache[key] = out
        return out

    if saw_answer:
        out = {"synced": None, "plain": None, "status": "absent"}
        cache[key] = out
        return out

    # Every attempt errored. Return empty but write NOTHING, so the next run
    # tries again instead of inheriting a phantom miss.
    return {"synced": None, "plain": None, "status": "error"}


def main():
    """Refresh the lyric cache for every proposed match, without Whisper.

    verify_lyrics.py skips tracks it has already verified, and the lyric fetch
    sits behind that skip, so re-running it will not repair a poisoned cache
    entry. This pass exists to do exactly that, and it is network-only: a few
    minutes rather than the ~20 that re-transcribing would cost.
    """
    import argparse
    import json
    import os
    import sys

    import requests

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only-missing", action="store_true",
                    help="only retry entries currently recorded as having "
                         "no lyrics")
    args = ap.parse_args()

    review = json.load(open(os.path.join(here, "cache", "review.json")))
    cpath = os.path.join(here, "cache", "lyrics.json")
    cache = json.load(open(cpath)) if os.path.exists(cpath) else {}

    durations = {}
    apath = os.path.join(here, "cache", "analysis.json")
    if os.path.exists(apath):
        for v in json.load(open(apath)).values():
            if v.get("path") and v.get("decoded_secs"):
                durations[v["path"]] = float(v["decoded_secs"])

    rows = [r for r in review if r.get("proposed_artist") and r.get("proposed_title")]
    if args.only_missing:
        def is_miss(r):
            e = cache.get(f'{r["proposed_artist"]}|{r["proposed_title"]}'.lower())
            return isinstance(e, dict) and not e.get("synced") and not e.get("plain")
        rows = [r for r in rows if is_miss(r)]
    if args.limit:
        rows = rows[: args.limit]

    before_s = sum(1 for v in cache.values()
                   if isinstance(v, dict) and v.get("synced"))
    session = requests.Session()
    gained, errors = 0, 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        key = f'{r["proposed_artist"]}|{r["proposed_title"]}'.lower()
        had = bool((cache.get(key) or {}).get("synced")) if isinstance(
            cache.get(key), dict) else False
        out = fetch(r["proposed_artist"], r["proposed_title"], cache, session,
                    album=r.get("proposed_album"),
                    duration=durations.get(r["path"]))
        if out.get("status") == "error":
            errors += 1
        elif out.get("synced") and not had:
            gained += 1
        if i % 25 == 0 or i == len(rows):
            el = time.time() - t0
            print(f"  {i}/{len(rows)}  {el:.0f}s  +{gained} synced  "
                  f"{errors} errors", flush=True)
            json.dump(cache, open(cpath + ".tmp", "w"),
                      ensure_ascii=False, indent=1)
            os.replace(cpath + ".tmp", cpath)

    json.dump(cache, open(cpath + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(cpath + ".tmp", cpath)
    after_s = sum(1 for v in cache.values()
                  if isinstance(v, dict) and v.get("synced"))
    print(f"\n  synced lyrics in cache: {before_s} -> {after_s} (+{after_s-before_s})")
    print(f"  unresolved errors (will retry next run): {errors}\n")
    return 1 if errors else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
