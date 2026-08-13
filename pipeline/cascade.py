#!/usr/bin/env python3
"""Iterative enrichment: every fact learned becomes a key for the next lookup.

The earlier stages are a straight line -- fingerprint, then text search, then
the streaming catalogues -- and each one throws away the fact that its output
is a better search key than its input was. A filename is a bad key. An artist
and title is a better one. A recording MBID is an exact one. An ISRC is exact
across every service on earth.

So this runs to a fixpoint instead of in sequence. Each resolver declares what
it NEEDS and what it GIVES; a track keeps running whichever resolvers have
their inputs satisfied and their outputs still missing, until a full pass adds
nothing new. Adding a source later means adding one entry to RESOLVERS, not
rewriting an order of operations.

The chain that matters most:

    artist+title -> recording MBID -> ISRC -> exact Deezer track -> BPM,
                                                                   contributors
                 -> album -> release-group MBID -> Cover Art Archive -> artwork

ISRC is the hinge. It is a unique identifier for a specific recording, so once
any source hands one over, every other source can be asked an exact question
rather than a fuzzy one -- which is what stops a cover or a remix being
mistaken for the original.

Every value carries the source that produced it, and a resolver never
overwrites a fact that an earlier, more trusted source already established.

Usage:
  cascade.py                  # every auto-accepted track
  cascade.py --limit 20
  cascade.py --tiers auto,review
  cascade.py --max-rounds 6
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.identify import RateLimiter  # noqa: E402
from pipeline.webmatch import fit, version_mismatch  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
OUT = os.path.join(HERE, "cache", "cascade.json")
from pipeline.useragent import UA  # noqa: E402

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"

MB_RATE = 0.85          # documented limit is 1 req/s per IP
DEEZER_RATE = 8.0
CAA_RATE = 4.0

# How much a source is believed when two disagree about the same field.
# `seed` is not a weak guess: it is everything the earlier stages established
# plus the rows the user confirmed by hand, so a streaming search must not be
# able to rewrite it. Deezer overwriting a correct 1992 album year with the
# 2007 reissue date is exactly what happens when the seed ranks below it.
TRUST = {"musicbrainz": 3, "coverartarchive": 3,
         "seed": 2, "deezer": 2, "itunes": 2}

# MusicBrainz answers 503 "currently busy" under load. Treating that as "no
# data" silently degrades every track to the streaming sources, so it is
# retried rather than swallowed.
MB_RETRY = {429, 500, 502, 503, 504}
MB_TRIES = 4

# How closely a candidate's album has to match the one we already hold before
# its artwork is believed to be this record's sleeve. The same 0.6 bar
# r_deezer_search already applies to artist and title, rather than a second
# number chosen separately: an album name is compared the same way a title is,
# and two thresholds for one kind of comparison drift apart.
MIN_ALBUM_FIT = 0.6

_YEAR = re.compile(r"(\d{4})")
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def year_of(s):
    m = _YEAR.search(str(s or ""))
    if not m:
        return None
    y = int(m.group(1))
    return y if 1900 <= y <= 2030 else None


def mb_escape(s):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s or "")


def mb_get(ctx, url, params, fx=None, what=""):
    """-> parsed JSON, or None. Retries the busy responses; a silent None
    would make the whole cascade quietly fall back to weaker sources."""
    for attempt in range(MB_TRIES):
        ctx["lims"]["mb"].wait()
        try:
            r = ctx["session"].get(url, params=params, headers=UA, timeout=25)
        except Exception as e:
            if attempt == MB_TRIES - 1 and fx is not None:
                fx.log.append(f"{what} unreachable: {str(e)[:50]}")
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        if r.status_code in MB_RETRY:
            time.sleep(1.5 * (attempt + 1))
            continue
        return None
    if fx is not None:
        ctx["stats"]["mb_gave_up"] += 1
        fx.log.append(f"{what} gave up after {MB_TRIES} tries (server busy)")
    return None


# ------------------------------------------------------------------ facts

class Facts:
    """A track's known fields, each with the source that established it."""

    def __init__(self, seed):
        self.f = {}
        for k, v in seed.items():
            if v not in (None, "", []):
                self.f[k] = {"value": v, "source": "seed"}
        self.log = []

    def get(self, key):
        e = self.f.get(key)
        return e["value"] if e else None

    def has(self, *keys):
        return all(self.get(k) is not None for k in keys)

    def put(self, key, value, source):
        if value in (None, "", []):
            return False
        cur = self.f.get(key)
        if cur is not None:
            # Never let a weaker source rewrite a stronger one's answer.
            if TRUST.get(source, 0) <= TRUST.get(cur["source"], 0):
                return False
            if cur["value"] == value:
                return False
        self.f[key] = {"value": value, "source": source}
        self.log.append(f"{key}={value} <- {source}")
        return True

    def dump(self):
        return {k: v["value"] for k, v in self.f.items()}

    def sources(self):
        return {k: v["source"] for k, v in self.f.items()}


# --------------------------------------------------------------- resolvers

def r_mb_recording(fx, ctx):
    """recording MBID -> ISRC, canonical names, length, genres, release."""
    d = mb_get(ctx, f"{MB}/recording/{fx.get('recording_id')}",
               {"fmt": "json",
                "inc": "artist-credits+isrcs+releases+release-groups+tags+genres"},
               fx, "mb-recording")
    if not d:
        return

    # A recording can carry several ISRCs, and they are not interchangeable:
    # one of Bon Jovi's "It's My Life" ISRCs resolves to a radio live session.
    # Keep them all and let the Deezer resolver try each until one comes back
    # as the same version we actually have.
    isrcs = [i for i in (d.get("isrcs") or []) if _ISRC.match(i or "")]
    if isrcs:
        fx.put("isrcs", isrcs, "musicbrainz")
        if len(isrcs) == 1:
            fx.put("isrc", isrcs[0], "musicbrainz")
    fx.put("title", d.get("title"), "musicbrainz")
    credits = d.get("artist-credit") or []
    names = [c["artist"]["name"] for c in credits
             if isinstance(c, dict) and c.get("artist")]
    if names:
        fx.put("artist", "; ".join(names), "musicbrainz")
        fx.put("artist_id", credits[0]["artist"]["id"], "musicbrainz")
    if d.get("length"):
        fx.put("length_ms", d["length"], "musicbrainz")

    genres = [g["name"] for g in (d.get("genres") or []) if g.get("count", 1) > 0]
    tags = [t["name"] for t in (d.get("tags") or [])]
    if genres or tags:
        fx.put("genres", genres or tags[:5], "musicbrainz")

    # Prefer the earliest release, and date it by the release GROUP's
    # first-release-date rather than the individual release's date. A pressing
    # of "Keep the Faith" sold in 2007 still belongs to a 1992 album, and
    # tagging it 2007 is how a reissue quietly rewrites a song's year.
    best, best_year = None, None
    for rel in d.get("releases") or []:
        rg = rel.get("release-group") or {}
        y = year_of(rg.get("first-release-date")) or year_of(rel.get("date"))
        if not y:
            continue
        # A compilation is only acceptable when nothing else is on offer.
        comp = (rg.get("primary-type") or "Album") == "Compilation"
        if comp and best is not None:
            continue
        if best_year is None or y < best_year:
            best, best_year = rel, y
    if best:
        rg = best.get("release-group") or {}
        fx.put("album", rg.get("title") or best.get("title"), "musicbrainz")
        fx.put("year", best_year, "musicbrainz")
        fx.put("release_group_id", rg.get("id"), "musicbrainz")
        fx.put("release_id", best.get("id"), "musicbrainz")
        fx.put("release_type", rg.get("primary-type"), "musicbrainz")


def r_mb_by_isrc(fx, ctx):
    """ISRC -> the exact MusicBrainz recording, no name matching involved."""
    d = mb_get(ctx, f"{MB}/isrc/{fx.get('isrc')}",
               {"fmt": "json", "inc": "artist-credits"}, fx, "mb-by-isrc")
    recs = (d or {}).get("recordings") or []
    if recs:
        fx.put("recording_id", recs[0].get("id"), "musicbrainz")


def r_deezer_by_isrc(fx, ctx):
    """ISRC -> the exact Deezer track: BPM, contributors, disc position.

    Tries every ISRC the recording carries and keeps the first whose title is
    the same version as ours, because "exact" identifiers can still point at a
    live session or a radio edit of the same song."""
    codes = fx.get("isrcs") or ([fx.get("isrc")] if fx.get("isrc") else [])
    for code in codes[:4]:
        ctx["lims"]["deezer"].wait()
        try:
            d = ctx["session"].get(
                f"https://api.deezer.com/track/isrc:{code}", timeout=20).json()
        except Exception:
            continue
        if d.get("error") or not d.get("id"):
            continue
        if _absorb_deezer_track(fx, d):
            fx.put("isrc", code, "musicbrainz")
            return


def put_cover(fx, album_title, cover_url, source):
    """Write artwork only when it belongs to the album this track carries.

    Artwork and the album name used to be written by adjacent lines that
    behaved differently for no stated reason. `album` loses to the seed and to
    MusicBrainz, which both outrank Deezer in TRUST; `cover_url` had no
    competing source at all, so it was written unconditionally. The track then
    carried one album's name and a different album's sleeve, and nothing
    anywhere compared them.

    It goes wrong exactly where the catalogue is thinnest. Deezer substitutes a
    compilation for an artist whose real albums it does not carry, every song
    by that artist matches into it, and every song inherits that compilation's
    cover: four Toše Proeski tracks with three different album tags all took
    the sleeve of a 36-track collection. Across the library, 23% of Balkan
    tracks with art shared an image with a track whose album tag differed,
    against 15% of the rest, and every one of them arrived through a
    text-matched streaming source. Cover Art Archive, which is keyed to a
    release MBID, produced none.

    Refusing here is not the same as going without. `cover_url` left unset is
    what makes the cover-art resolver eligible, since it only fires while its
    output is still missing, so declining a mismatched sleeve is what lets the
    authoritative source answer instead. On a 182-track sample, 47 of the 51
    refusals carried a release-group MBID for it to use.

    One function rather than one check per call site: the album resolver and
    the track resolver both write artwork, and a gate on only one of them
    measured as changing nothing at all.
    """
    if not cover_url:
        return False
    ours = fx.get("album")
    if not ours or fit(ours, album_title) >= MIN_ALBUM_FIT:
        return fx.put("cover_url", cover_url, source)
    fx.log.append(f"refused {source} cover: it belongs to "
                  f"'{album_title}', ours is '{ours}'")
    return False


def r_deezer_search(fx, ctx):
    """artist+title -> Deezer track id, when no ISRC exists to be exact with."""
    ctx["lims"]["deezer"].wait()
    artist, title = fx.get("artist"), fx.get("title")
    try:
        res = ctx["session"].get(
            "https://api.deezer.com/search",
            params={"q": f'artist:"{artist}" track:"{title}"', "limit": 5},
            timeout=20).json().get("data") or []
    except Exception:
        return
    for t in res:
        if fit(artist, (t.get("artist") or {}).get("name")) < 0.6:
            continue
        if fit(title, t.get("title")) < 0.6:
            continue
        # A remix must not inherit the original's facts, and vice versa.
        if version_mismatch(title, t.get("title")):
            continue
        fx.put("deezer_id", t.get("id"), "deezer")
        return


def r_deezer_track(fx, ctx):
    """Deezer track id -> the full track record."""
    ctx["lims"]["deezer"].wait()
    try:
        d = ctx["session"].get(
            f"https://api.deezer.com/track/{fx.get('deezer_id')}", timeout=20).json()
    except Exception:
        return
    if d.get("error"):
        return
    _absorb_deezer_track(fx, d)


def _absorb_deezer_track(fx, d):
    """-> True if the track was accepted. Rejects a different version of the
    song outright: inheriting a live take's album, year and track number is
    worse than having none of them."""
    ours = fx.get("title")
    missing = version_mismatch(ours, d.get("title")) if ours else None
    if missing:
        fx.log.append(f"rejected deezer '{d.get('title')}' - not the same "
                      f"version ({','.join(missing)})")
        return False
    fx.put("deezer_id", d.get("id"), "deezer")
    fx.put("title", d.get("title"), "deezer")
    fx.put("artist", (d.get("artist") or {}).get("name"), "deezer")
    contribs = [c.get("name") for c in (d.get("contributors") or []) if c.get("name")]
    if len(contribs) > 1:
        fx.put("all_artists", contribs, "deezer")
    if d.get("isrc") and _ISRC.match(d["isrc"]):
        fx.put("isrc", d["isrc"], "deezer")
    fx.put("track_number", d.get("track_position"), "deezer")
    fx.put("disc_number", d.get("disk_number"), "deezer")
    fx.put("explicit", bool(d.get("explicit_lyrics")), "deezer")
    # Deezer publishes its own tempo. Kept under a separate key so it is a
    # cross-check on our analysis, never a silent replacement for it.
    if d.get("bpm"):
        fx.put("bpm_deezer", round(float(d["bpm"]), 1), "deezer")
    if d.get("release_date"):
        fx.put("year", year_of(d["release_date"]), "deezer")
    alb = d.get("album") or {}
    if alb.get("id"):
        fx.put("deezer_album_id", alb["id"], "deezer")
    fx.put("album", alb.get("title"), "deezer")

    put_cover(fx, alb.get("title"), alb.get("cover_xl"), "deezer")
    return True


def r_deezer_album(fx, ctx):
    """Deezer album id -> label, genres, year, artwork."""
    ctx["lims"]["deezer"].wait()
    try:
        d = ctx["session"].get(
            f"https://api.deezer.com/album/{fx.get('deezer_album_id')}",
            timeout=20).json()
    except Exception:
        return
    if d.get("error"):
        return
    fx.put("album", d.get("title"), "deezer")
    fx.put("label", d.get("label"), "deezer")
    fx.put("year", year_of(d.get("release_date")), "deezer")
    fx.put("total_tracks", d.get("nb_tracks"), "deezer")
    put_cover(fx, d.get("title"), d.get("cover_xl"), "deezer")
    genres = [g.get("name") for g in ((d.get("genres") or {}).get("data") or [])
              if g.get("name")]
    if genres:
        fx.put("genres", genres, "deezer")


def r_mb_release_group(fx, ctx):
    """artist+album -> release-group MBID, which is the key to the artwork."""
    artist, album = fx.get("artist"), fx.get("album")
    q = f'releasegroup:"{mb_escape(album)}" AND artist:"{mb_escape(artist)}"'
    d = mb_get(ctx, f"{MB}/release-group",
               {"query": q, "fmt": "json", "limit": 5}, fx, "mb-release-group")
    for g in (d or {}).get("release-groups") or []:
        arts = "; ".join(a["artist"]["name"] for a in g.get("artist-credit", [])
                         if isinstance(a, dict) and a.get("artist"))
        if fit(artist, arts) < 0.6 or fit(album, g.get("title")) < 0.6:
            continue
        fx.put("release_group_id", g.get("id"), "musicbrainz")
        fx.put("year", year_of(g.get("first-release-date")), "musicbrainz")
        fx.put("release_type", g.get("primary-type"), "musicbrainz")
        return


def r_cover_art(fx, ctx):
    """release-group MBID -> Cover Art Archive front image."""
    ctx["lims"]["caa"].wait()
    rgid = fx.get("release_group_id")
    try:
        r = ctx["session"].get(f"{CAA}/release-group/{rgid}",
                               headers=UA, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            return
        images = r.json().get("images") or []
    except Exception:
        return
    for im in images:
        if im.get("front"):
            url = (im.get("thumbnails") or {}).get("500") or im.get("image")
            fx.put("cover_url", url, "coverartarchive")
            return


def r_mb_artist(fx, ctx):
    """artist MBID -> the artist's canonical name, country and aliases.

    This is what settles "Joško Čagalj Jole" versus "Jole": MusicBrainz keeps
    one primary name per artist plus every alias, so the same performer stops
    appearing under two spellings across the library."""
    d = mb_get(ctx, f"{MB}/artist/{fx.get('artist_id')}",
               {"fmt": "json", "inc": "aliases"}, fx, "mb-artist")
    if not d:
        return
    fx.put("artist_canonical", d.get("name"), "musicbrainz")
    fx.put("artist_country", d.get("country"), "musicbrainz")
    aliases = [a.get("name") for a in (d.get("aliases") or []) if a.get("name")]
    if aliases:
        fx.put("artist_aliases", aliases, "musicbrainz")


class Resolver:
    def __init__(self, name, needs, gives, fn):
        self.name, self.needs, self.gives, self.fn = name, needs, gives, fn

    def ready(self, fx):
        return (all(fx.get(k) is not None for k in self.needs)
                and any(fx.get(k) is None for k in self.gives))


# Order matters for cost, not for correctness -- the loop reaches the same
# fixpoint either way, but MusicBrainz allows one request a second while Deezer
# allows eight, so asking MusicBrainz first for facts Deezer would have handed
# over anyway turns a 20-minute run into an 8-hour one. Deezer leads; the
# MusicBrainz resolvers declare narrow `gives` so they only fire for what is
# genuinely still missing afterwards.
RESOLVERS = [
    Resolver("deezer-search", ("artist", "title"), ("deezer_id",), r_deezer_search),
    Resolver("deezer-track", ("deezer_id",),
             ("bpm_deezer", "isrc", "deezer_album_id", "track_number",
              "all_artists", "explicit"),
             r_deezer_track),
    Resolver("deezer-album", ("deezer_album_id",),
             ("label", "genres", "total_tracks", "cover_url"),
             r_deezer_album),
    Resolver("deezer-by-isrc", ("isrcs",),
             ("deezer_id", "bpm_deezer", "track_number"), r_deezer_by_isrc),
    Resolver("mb-recording", ("recording_id",),
             ("isrc", "genres", "year"), r_mb_recording),
    Resolver("mb-by-isrc", ("isrc",), ("recording_id",), r_mb_by_isrc),
    Resolver("mb-release-group", ("artist", "album"), ("year",),
             r_mb_release_group),
    Resolver("cover-art", ("release_group_id",), ("cover_url",), r_cover_art),
    Resolver("mb-artist", ("artist_id",),
             ("artist_canonical", "artist_aliases"), r_mb_artist),
]


def seed_of(row):
    """The identity a cached entry was grown from."""
    return {"artist": row.get("proposed_artist"),
            "title": row.get("proposed_title"),
            "album": row.get("proposed_album"),
            "year": row.get("proposed_year"),
            "recording_id": row.get("recording_id"),
            "release_group_id": row.get("release_group_id")}


def _stale(entry, row):
    """-> True when this row still needs enriching.

    Answering a review row with a link can replace the song outright, and
    everything the cascade resolved from the old identity (album, cover, ISRC,
    label) then describes the song that was replaced. Keying the cache on the
    path alone meant that entry was never revisited without --force.

    An entry written before this stamp existed carries no "seed" and is left
    alone: an upgrade should not silently re-query the whole library.
    """
    if entry is None:
        return True
    if "seed" not in entry:
        return False
    return entry["seed"] != seed_of(row)


def run_track(seed, ctx, max_rounds):
    """Keep firing whatever is ready until a whole pass changes nothing."""
    fx = Facts(seed)
    fired = []
    for _ in range(max_rounds):
        changed = False
        for res in RESOLVERS:
            if (res.name, tuple(sorted(fx.f))) in fired:
                continue
            if not res.ready(fx):
                continue
            before = dict(fx.f)
            fired.append((res.name, tuple(sorted(fx.f))))
            try:
                res.fn(fx, ctx)
            except Exception as e:
                fx.log.append(f"{res.name} failed: {str(e)[:60]}")
                continue
            if fx.f != before:
                changed = True
        if not changed:
            break
    return fx


# ------------------------------------------------------------------ driver

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tiers", default="auto")
    ap.add_argument("--max-rounds", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--only", metavar="PATHS",
                    help="a file of source paths, one per line: enrich only "
                         "these. For verifying a change against a frozen "
                         "sample, the way write_tags.py --only is used")
    args = ap.parse_args()

    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    rows = [r for r in json.load(open(REVIEW)) if r["tier"] in tiers]
    on_disk = json.load(open(OUT)) if os.path.exists(OUT) else {}
    # --force starts empty so that entries for tracks no longer in review.json
    # are dropped rather than kept forever. With --only that would throw away
    # every track the run was told NOT to look at, because the cache is
    # rewritten whole at the end: a 182-track verification would leave 182
    # entries where 1827 had been. So --only keeps what it is not rebuilding.
    cache = on_disk if (args.only or not args.force) else {}
    todo = [r for r in rows
            if args.force or _stale(on_disk.get(r["path"]), r)]
    if args.only:
        with open(args.only, encoding="utf-8") as fh:
            want = {ln.strip() for ln in fh if ln.strip()
                    and not ln.startswith("#")}
        # Named rather than silently ignored: a --only list whose paths match
        # nothing looks exactly like a run with nothing left to do, and the
        # difference is a verification that measured an empty set.
        missing = want - {r["path"] for r in rows}
        if missing:
            print(f"  {len(missing)} of {len(want)} paths are not in "
                  f"review.json and will not be enriched, e.g. "
                  f"{sorted(missing)[0]}")
        todo = [r for r in todo if r["path"] in want]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"  nothing to enrich ({len(cache)} cached)\n")
        return 0

    # MusicBrainz is the bottleneck at one request a second; extra workers only
    # help the resolvers that do not touch it.
    workers = args.workers or min(8, (os.cpu_count() or 4) + 2)
    ctx = {"lims": {"mb": RateLimiter(MB_RATE), "deezer": RateLimiter(DEEZER_RATE),
                    "caa": RateLimiter(CAA_RATE)},
           "session": requests.Session(),
           "stats": Counter()}

    print(f"  {len(todo)} tracks to enrich, {workers} workers, "
          f"{len(RESOLVERS)} resolvers ({len(cache)} cached)\n")
    lock, seen, t0 = threading.Lock(), {"n": 0}, time.time()
    gained = Counter()

    def work(r):
        fx = run_track(seed_of(r), ctx, args.max_rounds)
        return r, fx

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, r) for r in todo]
        for f in as_completed(futs):
            try:
                r, fx = f.result()
            except Exception as e:
                print(f"    worker failed: {str(e)[:90]}", flush=True)
                continue
            with lock:
                seen["n"] += 1
                cache[r["path"]] = {"file": r["file"], "facts": fx.dump(),
                                    "sources": fx.sources(), "trail": fx.log,
                                    # What these facts were grown from, so a
                                    # later run can tell that the row now
                                    # names a different song.
                                    "seed": seed_of(r)}
                for k, v in fx.sources().items():
                    if v != "seed":
                        gained[k] += 1
                if seen["n"] % 10 == 0 or seen["n"] == len(todo):
                    print(f"    [{seen['n']}/{len(todo)}] "
                          f"{r['file'][:40]:40} +{len(fx.log)} facts", flush=True)
                if seen["n"] % 50 == 0:
                    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
                    os.replace(OUT + ".tmp", OUT)

    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    n = max(seen["n"], 1)
    print(f"\n  {seen['n']} tracks in {time.time()-t0:.0f}s\n")
    print("  fields gained (not present in the seed):")
    for k, v in gained.most_common():
        print(f"    {k:20} {v:5}  ({100*v/n:3.0f}%)")
    print(f"\n  -> {OUT}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
