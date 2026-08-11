#!/usr/bin/env python3
"""Stage 4b: text search for tracks that acoustic fingerprinting could not place.

Fingerprints only exist for music somebody has already submitted, which means
regional catalogues (here: ex-Yu) come back empty. Text search finds those --
measured on this library, combining both sources lifts coverage from 48% to 65%.

Why not just let beets do it: beets issues 6-8 rate-limited MusicBrainz calls
per track (~17s each). A single `/ws/2/recording` search returns the recording,
artist credit, release, release-group type and date in one response, so this
does 1 call per source per track. MusicBrainz and Discogs are separate hosts
with separate limits, so the two run concurrently.

Usage: textsearch.py [--limit N] [--source mb|discogs|both]
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from pipeline.identify import (  # noqa: E402
    RateLimiter, TYPE_RANK, _BOOTLEG_DATE, _COMPILATION_HINT, _FEAT, norm, sim,
)
from pipeline.probe_match import split_name  # noqa: F401
from pipeline import tagseed  # noqa: E402

IDENT = os.path.join(HERE, "cache", "identity.json")
OUT = os.path.join(HERE, "cache", "textsearch.json")
SECRETS = os.path.join(HERE, "config", "secrets.json")

# MusicBrainz REQUIRES a descriptive User-Agent with contact info; generic
# agents get throttled harder or blocked outright.
from pipeline.useragent import UA  # noqa: E402
MB_URL = "https://musicbrainz.org/ws/2/recording"
DISCOGS_URL = "https://api.discogs.com/database/search"

# Sit just UNDER the documented ceilings. Running exactly at 1.0/s produced
# 25 MusicBrainz 503s and 66 Discogs 429s in one pass, and without retries
# those became silent "no candidate" results.
MB_RATE = 0.85       # documented limit is 1 req/s per IP
DISCOGS_RATE = 0.8   # documented limit is 60/min authenticated
MAX_RETRIES = 4


# Discogs disambiguates same-named artists with a trailing number, and marks
# "artist name variations" with an asterisk: "Corona (18)", "Doris*". Both are
# database bookkeeping, not part of the name, and must not reach a tag.
_DISCOGS_ARTEFACT = re.compile(r"\s*\((\d{1,3})\)|\*")


def clean_artist(name):
    if not name:
        return name
    out = _DISCOGS_ARTEFACT.sub("", name)
    return re.sub(r"\s{2,}", " ", out).strip(" ,;-")


def mb_escape(s):
    """Lucene special characters would otherwise break the query."""
    return "".join("\\" + c if c in r'+-&|!(){}[]^"~*?:\\/' else c for c in s)


def get_with_retry(session, url, limiter, **kw):
    """GET with backoff on the transient rejections these APIs actually emit.

    429 (rate limited) and 5xx are temporary; treating them as failure loses
    real matches. Honour Retry-After when the server sends it.
    """
    delay = 2.0
    for attempt in range(MAX_RETRIES):
        limiter.wait()
        try:
            r = session.get(url, timeout=40, **kw)
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            if attempt == MAX_RETRIES - 1:
                return r
            wait = delay
            ra = r.headers.get("Retry-After")
            if ra:
                try:
                    wait = max(wait, float(ra))
                except ValueError:
                    pass
            time.sleep(wait)
            delay *= 2
            continue
        return r
    return r


def rank_release(rel):
    """Lower is better. Same ranking logic as the fingerprint path."""
    rg = rel.get("release-group") or {}
    rtype = rg.get("primary-type")
    title = rg.get("title") or rel.get("title") or ""
    penalty = 0
    if rg.get("secondary-types"):
        penalty += 4
    if _COMPILATION_HINT.search(title):
        penalty += 3
    if _BOOTLEG_DATE.match(title):
        penalty += 6
    date = rel.get("date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None
    return TYPE_RANK.get(rtype, 7) + penalty, year or 9999, title, rg.get("id"), rtype, year


def best_release(rec):
    rels = rec.get("releases") or []
    if not rels:
        return None
    best = min((rank_release(r) for r in rels), key=lambda t: (t[0], t[1]))
    return {"album": best[2], "release_group_id": best[3],
            "type": best[4], "year": best[5]}


def name_fit(rec_artists, rec_title, orders):
    best = (-1.0, 0.0, 0.0)
    for a, t in orders:
        aa = max((sim(a, x) for x in rec_artists), default=0.0) if a else 0.0
        tt = sim(t, rec_title) if t else 0.0
        fit = (0.4 * aa + 0.6 * tt) if a else tt
        if fit > best[0]:
            best = (fit, aa, tt)
    return best


def orders_for(artist, title):
    o = [(artist, title)]
    if artist and title:
        o.append((title, artist))
    for feat in _FEAT.findall(title or ""):
        o.append((feat.strip(" ()[]"), title))
    return o


def search_mb(name, limiter, session, path=None):
    artist, title, _trust = tagseed.seed_for(path or "", os.path.splitext(name)[0])
    if not title:
        return None
    if artist:
        q = f'recording:"{mb_escape(title)}" AND artist:"{mb_escape(artist)}"'
    else:
        q = f'recording:"{mb_escape(title)}"'
    try:
        r = get_with_retry(session, MB_URL, limiter,
                           params={"query": q, "fmt": "json", "limit": 5},
                           headers=UA)
        if r.status_code != 200:
            return {"error": f"mb HTTP {r.status_code}"}
        recs = r.json().get("recordings") or []
    except Exception as e:
        return {"error": f"mb {type(e).__name__}: {str(e)[:80]}"}

    orders = orders_for(artist, title)
    cands = []
    for rec in recs:
        arts = [a["artist"]["name"] for a in rec.get("artist-credit", [])
                if isinstance(a, dict) and a.get("artist")]
        fit, a_sim, t_sim = name_fit(arts, rec.get("title"), orders)
        # MusicBrainz' own relevance score is a useful prior but must not
        # override the filename: searching a title alone happily returns a
        # score-100 recording by a completely different artist.
        mbscore = float(rec.get("score") or 0) / 100.0
        combined = 0.3 * mbscore + 0.7 * fit
        cands.append({
                "source": "musicbrainz",
                "recording_id": rec.get("id"),
                "title": rec.get("title"),
                "artist": "; ".join(arts) if arts else None,
                "search_score": round(mbscore, 3),
                "artist_similarity": round(a_sim, 3),
                "title_similarity": round(t_sim, 3),
                "combined": round(combined, 3),
                "release": best_release(rec),
        })
    if not cands:
        return None
    # Keep the runners-up: the review UI needs "pick one of these" rather than
    # a blank text field. Recomputing them means re-running every lookup.
    cands.sort(key=lambda c: -c["combined"])
    best = cands[0]
    best["alternatives"] = [c for c in cands[1:4]]
    return best


def search_discogs(name, limiter, session, token, path=None):
    artist, title, _trust = tagseed.seed_for(path or "", os.path.splitext(name)[0])
    if not title:
        return None
    q = f"{artist} {title}" if artist else title
    try:
        r = get_with_retry(session, DISCOGS_URL, limiter,
                           params={"q": q, "type": "release", "per_page": 5},
                           headers={**UA, "Authorization": f"Discogs token={token}"})
        if r.status_code != 200:
            return {"error": f"discogs HTTP {r.status_code}"}
        results = r.json().get("results") or []
    except Exception as e:
        return {"error": f"discogs {type(e).__name__}: {str(e)[:80]}"}

    orders = orders_for(artist, title)
    dcands = []
    for res in results:
        # Discogs returns "Artist - Album" in one string.
        full = res.get("title") or ""
        d_artist, _, d_album = full.partition(" - ")
        d_artist = clean_artist(d_artist)
        # A compilation credits the RELEASE to "Various", which says nothing
        # about who performed this track. Keeping it produced the worst-scored
        # rows in the whole review queue -- correct identifications wearing a
        # useless name (THCF, Feminnem, MC Yankoo all became "Various"). The
        # searched-for artist is the better answer; the release still records
        # that it is a compilation.
        compilation = d_artist.strip().lower() in ("various", "various artists")
        if compilation:
            d_artist = artist or d_artist
        fit, a_sim, t_sim = name_fit([d_artist], d_album, orders)
        dcands.append({
                "source": "discogs",
                "recording_id": None,
                "title": title,
                "artist": d_artist or None,
                "search_score": None,
                "artist_similarity": round(a_sim, 3),
                "title_similarity": round(t_sim, 3),
                "combined": round(fit, 3),
                # Discogs "format" is the physical medium (CD, Vinyl,
                # Cassette, File), NOT a release type. Storing it as `type`
                # made review.py penalise 170 rows for "album is a CD", 87 of
                # which had no other complaint at all.
                "release": {"album": d_album or None,
                            "release_group_id": None,
                            "type": "Compilation" if compilation else None,
                            "format": res.get("format", [None])[0]
                            if res.get("format") else None,
                            "year": int(res["year"]) if str(res.get("year", "")).isdigit() else None},
                "discogs_id": res.get("id"),
                "country": res.get("country"),
                "label": (res.get("label") or [None])[0],
        })
    if not dcands:
        return None
    dcands.sort(key=lambda c: -c["combined"])
    best = dcands[0]
    best["alternatives"] = [c for c in dcands[1:4]]
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--source", choices=["mb", "discogs", "both"], default="both")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ident = json.load(open(IDENT))
    done = {} if args.force or not os.path.exists(OUT) else json.load(open(OUT))
    todo = [(p, e) for p, e in ident.items()
            if not e.get("match") and p not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"  {len(todo)} unmatched tracks to text-search "
          f"({len(done)} already cached)")
    if not todo:
        print("  nothing to do\n")
        return

    token = json.load(open(SECRETS)).get("discogs_token")
    if not token:
        try:
            import yaml
            token = yaml.safe_load(open(os.path.join(HERE, "config", "config.yaml"))
                                   )["discogs"]["user_token"]
        except Exception:
            token = None

    mb_lim, dc_lim = RateLimiter(MB_RATE), RateLimiter(DISCOGS_RATE)
    session = requests.Session()
    t0 = time.time()

    def work(item):
        path, e = item
        out = {"name": e["name"], "path": path}
        # Two hosts, two independent rate limits -> genuinely concurrent.
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_mb = ex.submit(search_mb, e["name"], mb_lim, session, path) \
                if args.source in ("mb", "both") else None
            f_dc = ex.submit(search_discogs, e["name"], dc_lim, session, token, path) \
                if (args.source in ("discogs", "both") and token) else None
            out["mb"] = f_mb.result() if f_mb else None
            out["discogs"] = f_dc.result() if f_dc else None
        cands = [c for c in (out["mb"], out["discogs"])
                 if c and not c.get("error")]
        out["best"] = max(cands, key=lambda c: c["combined"]) if cands else None
        return out

    # Bounded concurrency: the rate limiters do the real throttling, the pool
    # just keeps enough requests in flight to hide latency.
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, res in enumerate(ex.map(work, todo), 1):
            done[res["path"]] = res
            if i % 20 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"  {i}/{len(todo)}  {el:.0f}s  {el/i:.2f}s/track "
                      f"eta {(len(todo)-i)*el/i:.0f}s", flush=True)
                json.dump(done, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
                os.replace(OUT + ".tmp", OUT)

    json.dump(done, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)

    vals = list(done.values())
    got = [v for v in vals if v.get("best")]
    strong = [v for v in got if v["best"]["combined"] >= 0.75]
    from collections import Counter
    print(f"\n  text-searched {len(vals)}   found candidate {len(got)} "
          f"({100*len(got)/max(len(vals),1):.0f}%)   strong {len(strong)}")
    print("  winning source:", dict(Counter(v["best"]["source"] for v in got)))
    print(f"  wall {time.time()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
