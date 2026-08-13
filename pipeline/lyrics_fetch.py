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
import unicodedata

# LRCLIB is a free, donation-funded community service with no published limit.
# Two per second is brisk enough to finish 3000 tracks in ~25 minutes and light
# enough not to be rude.
RATE = 2.0
MAX_RETRIES = 4
RETRY_STATUS = {429, 500, 502, 503, 504}

# The agreement below which a name is a different name. Shared by the picker,
# the fallback rule and write_tags' trust gate so all three draw the line in
# the same place. Defined next to fit() in webmatch and re-exported here,
# because importing this module to read one float pulls in the HTTP client.
from pipeline.webmatch import MIN_FIT  # noqa: F401  re-exported

# Which selection rules produced a cache entry. Bump when a change would pick
# a *different* hit than the entry already holds, so entries chosen by the old
# rules can be found and re-judged instead of being trusted forever.
#
#   1  original: ranked on (artist, synced, duration). No title comparison,
#      and a wrong-artist hit could ship as a fallback.
#   2  title compared in the picker, wrong-artist fallbacks recorded as such,
#      and artist_fit/title_fit stamped on every entry.
SELECTOR = 2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# _UNDECOMPOSED is shared rather than copied: a query variant that folded a
# letter differently from the comparison that judges the answer would ask for
# one spelling and then reject what came back.
from pipeline.webmatch import fit, _UNDECOMPOSED  # noqa: E402

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


def deaccent(s):
    """-> the same words with the diacritics taken off, or None if unchanged.

    Query-side only, and deliberately not the same job as webmatch.norm(). That
    one folds both sides of a comparison we control, so it can flatten
    punctuation and case too. This one has to produce something a human might
    have typed into LRCLIB's upload form, so it keeps the spacing and
    punctuation and only removes the marks: "Neću" -> "Necu", and the stroked
    d follows the same "dj" romanization norm() uses, "Rođendan" -> "Rodjendan".

    Worth having because our comparison already folds these but our *questions*
    do not. Measured on this library: titles carrying diacritics are missing
    synced lyrics at 27.1% against 14.1% for titles without, which is a search
    failure and not an absence -- LRCLIB holds whichever spelling the uploader
    used, and plenty of them do not type the marks.
    """
    if not s:
        return None
    out = unicodedata.normalize("NFKD", s.translate(_UNDECOMPOSED))
    out = "".join(c for c in out if not unicodedata.combining(c))
    return out if out != s else None


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
    bare = deaccent(title)
    if bare:
        out.append(bare)
    seen, uniq = set(), []
    for t in out:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq


def artist_variants(artist):
    """Full credit first, then the lead artist alone, then both unaccented."""
    if not artist:
        return []
    out = [artist]
    parts = [p for p in _ARTIST_SPLIT.split(artist) if p.strip()]
    if parts and parts[0].strip() != artist.strip():
        out.append(parts[0].strip())
    # Same reason as title_variants: LRCLIB holds whichever spelling the
    # uploader typed, and "Halid Beslic" is filed far more often than the
    # correctly accented form.
    for a in list(out):
        bare = deaccent(a)
        if bare:
            out.append(bare)
    seen, uniq = set(), []
    for a in out:
        if a.lower() not in seen:
            seen.add(a.lower())
            uniq.append(a)
    return uniq


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


def _pick(hits, duration, artist=None, title=None):
    """Prefer the right artist, then the right song, then synced, then duration.

    LRCLIB routinely holds several entries for one song (7 for Rasta's
    "Euforija"). Taking the first is arbitrary; taking the nearest duration
    picks the edit we actually have, which is what keeps the timestamps aligned.

    Artist has to come first, though. Ranking on duration alone fetched
    JoelB's "MAMBA" for Grse's, and a Polish "Kawasaki" for Rasta's -- whole
    songs in the wrong language, embedded as if they were right, and then used
    to decide the track's language too.

    Title has to come second, and it did not used to be here at all. Ranking
    the right artist's hits by duration alone picks whichever of their songs is
    closest in runtime, which is a different song sung by the right voice --
    the hardest kind of wrong to notice, because the language and the singer
    are both correct. Measured on this library: 15 sidecars carried another
    song by the correct artist. A duration check cannot catch these, and it is
    not a small residue: of 94 wrong matches, only 58 also failed a 5 s
    duration gate, so 36 needed this comparison specifically.
    """
    if not hits:
        return None

    def score(h):
        d = h.get("duration")
        off = abs(d - duration) if (d and duration) else 999
        wrong_artist = wrong_title = 0
        if artist and h.get("artistName"):
            wrong_artist = 0 if fit(artist, h["artistName"]) >= MIN_FIT else 1
        if title and h.get("trackName"):
            wrong_title = 0 if fit(title, h["trackName"]) >= MIN_FIT else 1
        return (wrong_artist, wrong_title,
                0 if h.get("syncedLyrics") else 1, off)
    return sorted(hits, key=score)[0]


def _rescore(entry, artist, title):
    """-> the entry with artist_fit/title_fit filled in, or None if it cannot be.

    An entry that recorded which hit it took can be re-judged against stricter
    rules with no network call at all: the names are already in 'matched'. That
    is what keeps a selector bump cheap. Only entries that fail the new rules,
    or that never recorded a hit, have to go back to LRCLIB.
    """
    matched = entry.get("matched") or ""
    if " - " not in matched:
        return None                       # legacy shape, nothing to judge
    ma, _, mt = matched.partition(" - ")
    entry["artist_fit"] = round(fit(artist, ma), 3)
    entry["title_fit"] = round(fit(title, mt), 3)
    return entry


def _good(entry):
    """-> True when a re-scored entry still passes the current rules.

    A plain-only hit is not final. The deaccented variants this selector adds
    are exactly the queries that find the synced sheet a diacritic hid, and a
    title carrying diacritics misses synced lyrics at 27.1% against 14.1%
    without them. Some of those misses are cached as plain-only `ok` entries
    rather than as `absent`, so freezing them here would put the timings out
    of reach for good. Re-asking costs one search.
    """
    return (entry.get("artist_fit", 0) >= MIN_FIT
            and entry.get("title_fit", 0) >= MIN_FIT
            and bool(entry.get("synced")))


def _from_other_sources(artist, title, duration):
    """-> a candidate, None, or lyric_sources.ERROR.

    Order is by what the answer is worth: timed lyrics that fit the file
    first, then any timed lyrics, then plain words. Untimed words beat timed
    words belonging to a different song, which is the whole reason this comes
    after LRCLIB rather than instead of it.

    ERROR when a source could not be asked and no other source answered. The
    caller must not record an absence on that, for the same reason LRCLIB's
    own errors have never been cached.
    """
    from pipeline import lyric_sources
    failed = False
    got = lyric_sources.from_ytmusic(artist, title, duration)
    if got is lyric_sources.ERROR:
        failed, got = True, None
    if got and got.get("synced"):
        # Its timings were written for ITS copy of the song. A file that is
        # a different length carries a different edit, so keep the words and
        # drop the timings rather than ship subtitles that drift.
        md, dur = got.get("matched_duration"), duration
        if md and dur and abs(md - dur) > lyric_sources.MAX_DRIFT:
            got["synced"] = None
            got["timing_dropped"] = f"{abs(md - dur):.0f}s from this file"
    if got and (got.get("synced") or got.get("plain")):
        return got
    token = _genius_token()
    if token:
        g = lyric_sources.from_genius(artist, title, token)
        if g is lyric_sources.ERROR:
            failed = True
        elif g:
            return g
    return lyric_sources.ERROR if failed else None


def _genius_token():
    """The key beets already takes inline, read once."""
    if not hasattr(_genius_token, "value"):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            import json as _json
            with open(os.path.join(root, "config", "secrets.json")) as fh:
                _genius_token.value = _json.load(fh).get("genius_access_token")
        except Exception:
            _genius_token.value = None
    return _genius_token.value


def fetch(artist, title, cache, session, album=None, duration=None):
    """-> {"synced": str|None, "plain": str|None, "status": "ok"|"absent"}.

    A cached entry is reused only when it is definitive AND was chosen by the
    current selection rules. Legacy entries (which carry no 'status') that
    recorded nothing are retried, which is what repairs the 62 false negatives
    already sitting in the cache.

    Being definitive is not enough on its own. Every entry here was once
    chosen by a picker that never compared the title, and the early return
    below meant no amount of re-running would ever revisit one -- the same
    shape of bug as the cached false negatives this module was written to fix,
    one level up: there the wrong *answer* was frozen, here the wrong *rule*
    is. So entries carry the selector that produced them, and an entry from an
    older selector is re-judged before it is trusted.

    Re-judging is free where the entry recorded which hit it took. Only what
    fails, or never recorded one, costs a request.
    """
    key = f"{artist}|{title}".lower()
    cached = cache.get(key)
    if isinstance(cached, dict):
        current = cached.get("selector") == SELECTOR
        if current and cached.get("status") in ("ok", "absent"):
            return cached
        if not current and cached.get("status") == "ok":
            # Chosen by older rules. Judge it where we can do so for nothing.
            rescored = _rescore(dict(cached), artist, title)
            if rescored and _good(rescored):
                rescored["selector"] = SELECTOR
                cache[key] = rescored
                return rescored
            # Failed, or unjudgeable: fall through and ask again.
        elif current is False and cached.get("status") == "absent":
            # Nothing was found under the old query set. The variants added
            # alongside this selector are new questions, so ask them.
            pass
        elif cached.get("synced") or cached.get("plain"):
            cached.setdefault("status", "ok")       # legacy hit, still good
            rescored = _rescore(dict(cached), artist, title)
            if rescored and _good(rescored):
                rescored["selector"] = SELECTOR
                cache[key] = rescored
                return rescored
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
    near = None
    for path, params in attempts:
        status, payload = _get(session, path, params)
        if status == "error":
            continue                      # do NOT let a failure imply absence
        saw_answer = True
        if status == "absent":
            continue
        hit = _pick(_hits_from(payload), duration, artist, title)
        if not (hit and (hit.get("syncedLyrics") or hit.get("plainLyrics"))):
            continue
        right_artist = not artist or not hit.get("artistName") or \
            fit(artist, hit["artistName"]) >= MIN_FIT
        right_title = not title or not hit.get("trackName") or \
            fit(title, hit["trackName"]) >= MIN_FIT
        # Stopping at the first attempt that returns anything is how a Polish
        # "Kawasaki" won: it was found by an earlier query than the Serbian
        # "Kavasaki" spelling that is actually this song. Keep an imperfect hit
        # only as a fallback and carry on looking for a better one.
        if right_artist and right_title:
            best = hit
            if hit.get("syncedLyrics"):
                break                     # the right song, synced: done
        elif right_artist and best is None:
            # The right artist, some other song of theirs. Kept only as a
            # last resort and marked, because it is the shape that put
            # "Kajem Se" on "Porok": LRCLIB has no Porok at all, the broad
            # q= query answered with another Katarina Zivkovic song, and it
            # was stored as though it were the answer. write_tags refuses it
            # at the gate, so keeping it buys nothing except a wrong-looking
            # cache; it stays only so the audit can see what was offered.
            near = hit

    # LRCLIB had nothing for this song. Ask the other catalogues before
    # recording an absence: LRCLIB is thin outside English, which is most of
    # this library, and YouTube Music carries the same songs timed.
    if best is None:
        from pipeline import lyric_sources
        alt = _from_other_sources(artist, title, duration)
        if alt is lyric_sources.ERROR:
            # A source we could not reach is not a source that said no.
            # Suppress every absence below so nothing is written and the next
            # run asks again, exactly as an LRCLIB error already does.
            saw_answer, near, alt = False, None, None
        if alt:
            alt.update({"status": "ok", "selector": SELECTOR,
                        "artist_fit": fit(artist, alt["matched"].partition(" - ")[0])
                        if artist else None,
                        "title_fit": fit(title, alt["matched"].partition(" - ")[2])
                        if title else None})
            cache[key] = alt
            return alt

    if best is None and near is not None:
        # Nothing matched this song. A blank field beats a wrong one, so the
        # entry records what was offered and refuses it, instead of storing
        # another song's words under this song's name.
        na = near.get("artistName") or ""
        nt = near.get("trackName") or ""
        cache[key] = {"synced": None, "plain": None, "status": "absent",
                      "selector": SELECTOR, "why": "only a different song",
                      "nearest": f"{na} - {nt}"}
        return cache[key]

    if best:
        ma = best.get("artistName") or ""
        mt = best.get("trackName") or ""
        out = {"synced": best.get("syncedLyrics") or None,
               "plain": best.get("plainLyrics") or None,
               "status": "ok",
               "matched": f"{ma} - {mt}",
               "matched_duration": best.get("duration"),
               # Stamped rather than recomputed downstream, so write_tags and
               # the audit grade the entry on the same numbers the picker used.
               # None, not 0.0, where the hit named nothing: the gates above
               # accept a hit with no artistName on purpose, and fit(x, "") is
               # 0.0, which the write side would read as a measured
               # disagreement and throw the lyrics away.
               "artist_fit": round(fit(artist, ma), 3) if (artist and ma) else None,
               "title_fit": round(fit(title, mt), 3) if (title and mt) else None,
               "selector": SELECTOR}
        cache[key] = out
        return out

    if saw_answer:
        out = {"synced": None, "plain": None, "status": "absent",
               "selector": SELECTOR}
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
