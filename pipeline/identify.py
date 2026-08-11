#!/usr/bin/env python3
"""Stage 4a: resolve identity with ONE AcoustID call per track.

Why this exists: letting beets search MusicBrainz costs 6-8 rate-limited
requests per track (~17s). A single AcoustID lookup with
`meta=recordings releasegroups releases` returns the recording MBID, artists,
and every release group with type and year -- everything we need -- in one
request, and AcoustID permits 3 req/s rather than MusicBrainz's 1.

Measured: 17s/track -> ~1.2s/track here, and beets then does a direct ID
lookup at ~4s/track instead of searching.

The catch, and the reason this file is more than a loop: AcoustID's
top-scoring result is not always the right one. A file named
"Arctic Monkeys - Do I Wanna Know" returned Hozier's BBC Live Lounge cover at
score 0.995. Fingerprints identify *audio*, and covers/live versions are
different audio that legitimately matches a different recording. So we score
every candidate against the filename and prefer agreement over raw score.

Usage: identify.py [--limit N] [-j workers]
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from pipeline import tagseed  # noqa: E402

FP_CACHE = os.path.join(HERE, "cache", "fingerprints.json")
OUT = os.path.join(HERE, "cache", "identity.json")
SECRETS = os.path.join(HERE, "config", "secrets.json")

API = "https://api.acoustid.org/v2/lookup"
META = "recordings releasegroups releases compress"
RATE = 2.5          # AcoustID documents a hard 3/s ceiling; stay under it.

# Release-group types, best first. An original studio album beats a
# compilation, which beats a single -- that ordering is what makes
# "what album is this from?" give a useful answer instead of "Now That's
# What I Call Music 47".
TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2, "Compilation": 5,
             "Live": 6, "Soundtrack": 6, "Remix": 6, "Broadcast": 8,
             "Other": 8, None: 7}


class RateLimiter:
    """Token bucket shared across threads."""

    def __init__(self, per_sec):
        self._interval = 1.0 / per_sec
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if self._next < now:
                self._next = now
            delay = self._next - now
            self._next += self._interval
        if delay > 0:
            time.sleep(delay)


def norm(s):
    """Casefold, strip accents and punctuation, for fuzzy name comparison."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# Titles that betray a compilation even when MusicBrainz marks them "Album"
# and gives them no secondary type. Promo/radio-service discs are the worst
# offenders because they are often the *earliest* release of a track.
_COMPILATION_HINT = re.compile(
    r"""(promo\ only|now\ that'?s|now\ \d+|ultimate|greatest\ hits|
        \bhits\b|anthems|classics|essential|the\ best\ of|best\ of\ \d{4}|
        \bvol\.?\s*\d|\bvolume\s*\d|compilation|sampler|megamix|
        \d{2,3}%\ hits|top\ \d+|mixtape|radio\ \d)""",
    re.IGNORECASE | re.VERBOSE,
)
# Bootleg/live recordings are usually titled with the gig date.
_BOOTLEG_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")
# Featured artists carry real identifying signal, especially on title-only files.
_FEAT = re.compile(r"(?:feat\.?|ft\.?|featuring|with)\s+([^()\[\]]+)", re.IGNORECASE)


def pick_release_group(rec):
    """Best release group: the album the track is actually *from*.

    Ranking beats naive "earliest release" because promo/radio compilations
    frequently predate the studio album. Arctic Monkeys' "Do I Wanna Know?"
    resolves to "Promo Only: Modern Rock Radio" on year alone; it should be "AM".
    """
    rec_artists = {norm(a.get("name")) for a in (rec.get("artists") or [])
                   if a.get("name")}
    best = None
    for rg in rec.get("releasegroups") or []:
        rtype = rg.get("type")
        years = [r.get("date", {}).get("year")
                 for r in (rg.get("releases") or []) if r.get("date")]
        years = [y for y in years if y]
        year = min(years) if years else None
        title = rg.get("title") or ""

        penalty = 0
        if rg.get("secondarytypes"):          # Compilation / Live / Soundtrack
            penalty += 4
        rg_artists = {norm(a.get("name")) for a in (rg.get("artists") or [])
                      if a.get("name")}
        if "various artists" in rg_artists:
            penalty += 4
        elif rg_artists and rec_artists and not (rg_artists & rec_artists):
            penalty += 2                      # credited to someone else entirely
        if _COMPILATION_HINT.search(title):
            penalty += 3

        if _BOOTLEG_DATE.match(title):        # "2014-08-23: Live at Reading"
            penalty += 6

        rank = TYPE_RANK.get(rtype, 7) + penalty
        cand = (rank, year or 9999)
        if best is None or cand < best[0]:
            best = (cand, title, rg.get("id"), rtype, year)
    if not best:
        return None
    return {"album": best[1], "release_group_id": best[2],
            "type": best[3], "year": best[4], "_rank": best[0][0],
            "_year_sort": best[0][1]}


def choose(results, want_artist, want_title, name_trust="filename"):
    """Pick the best (result, recording) pair using audio score + filename fit.

    Fingerprint score alone picks covers. Name similarity alone picks whatever
    the filename says even when the audio disagrees. Combining them, weighted
    toward the name when we actually have one, is what resolves the Hozier case.
    """
    have_name = bool(want_title)
    # Filenames come in both "Artist - Title" and "Title - Artist" order, and
    # guessing wrong tanks the similarity score on a perfectly correct match
    # ("Zbog mene ne placi - PRLJAVO KAZALISTE"). Try both and keep the better
    # reading; a genuine mismatch scores badly either way.
    orders = [(want_artist, want_title)]
    if want_artist and want_title:
        orders.append((want_title, want_artist))
    # "Bad Habits (feat. Bring Me The Horizon).mp3" has no lead artist, but the
    # featured act is right there and corroborates Ed Sheeran's recording.
    # Treat it as an artist candidate rather than calling the track unverifiable.
    for feat in _FEAT.findall(want_title or ""):
        orders.append((feat.strip(" ()[]"), want_title))

    cands = []
    for res in results:
        fscore = float(res.get("score") or 0)
        for rec in res.get("recordings") or []:
            artists = [a.get("name") for a in (rec.get("artists") or []) if a.get("name")]
            best_fit = (-1.0, 0.0, 0.0)
            for cand_artist, cand_title in orders:
                aa = max((sim(cand_artist, a) for a in artists), default=0.0) if cand_artist else 0.0
                tt = sim(cand_title, rec.get("title")) if cand_title else 0.0
                fit = (0.4 * aa + 0.6 * tt) if cand_artist else tt
                if fit > best_fit[0]:
                    best_fit = (fit, aa, tt)
            name_fit, a_sim, t_sim = best_fit
            # Weight the filename heavily when we have one: it is the only
            # thing that distinguishes an original from a cover.
            # A name we do not trust (an uploader channel rather than an
            # artist) must not outvote the audio, so it gets roughly half the
            # usual pull; a trusted tag gets slightly more than a filename.
            if not have_name:
                combined = fscore
            else:
                w = {"weak": 0.35, "filename": 0.65,
                     "tag": 0.70, "description": 0.75, "override": 0.75}.get(
                         name_trust, 0.65)
                combined = (1.0 - w) * fscore + w * name_fit
            rel = pick_release_group(rec)
            cands.append({
                "recording_id": rec.get("id"),
                "title": rec.get("title"),
                "artist": "; ".join(artists) if artists else None,
                "fingerprint_score": round(fscore, 3),
                "artist_similarity": round(a_sim, 3),
                "title_similarity": round(t_sim, 3),
                "combined": round(combined, 3),
                "release": rel,
            })
    if not cands:
        return None

    # MusicBrainz holds several near-identical recordings of the same track,
    # and they tie exactly on score and name. Whichever we saw first used to
    # win, which is how "Do I Wanna Know?" landed on a promo-radio compilation
    # instead of "AM". Round the score so near-ties are ties, then let album
    # quality decide.
    def sort_key(c):
        rel = c.get("release") or {}
        return (
            -round(c["combined"], 2),                 # best score band first
            rel.get("_rank", 99),                     # real album beats promo
            rel.get("_year_sort", 9999),              # then earliest pressing
            0 if rel.get("album") else 1,             # having an album at all
        )

    ranked = sorted(cands, key=sort_key)

    def strip_private(c):
        if c.get("release"):
            c["release"] = {k: v for k, v in c["release"].items()
                            if not k.startswith("_")}
        return c

    best = strip_private(ranked[0])
    best["n_candidates"] = len(cands)
    # Keep the runners-up. The review UI needs "here are the other options,
    # pick one" -- otherwise rejecting a bad match means typing it out by hand.
    # Recomputing these later would mean re-running every lookup, so the few
    # bytes are worth it. Deduplicated by recording id.
    seen, alts = {best.get("recording_id")}, []
    for c in ranked[1:]:
        rid = c.get("recording_id")
        if rid in seen:
            continue
        seen.add(rid)
        alts.append(strip_private(c))
        if len(alts) == 4:
            break
    best["alternatives"] = alts
    return best


def lookup(entry, key, limiter, session):
    name = entry["name"]
    stem = os.path.splitext(name)[0]
    # Embedded tags beat the filename when we have them: Namida writes a real
    # artist and title, and tagseed has already replaced channel names with the
    # artist the upload's own description credits.
    want_artist, want_title, name_trust = tagseed.seed_for(entry["path"], stem)
    try:
        limiter.wait()
        r = session.post(API, data={
            "client": key,
            "duration": int(entry["duration"]),
            "fingerprint": entry["fingerprint"],
            "meta": META,
        }, timeout=40)
        if r.status_code != 200:
            return {"name": name, "path": entry["path"],
                    "error": f"HTTP {r.status_code}"}
        d = r.json()
        if d.get("status") != "ok":
            return {"name": name, "path": entry["path"],
                    "error": str(d.get("error"))[:120]}
        results = d.get("results") or []
        chosen = choose(results, want_artist, want_title, name_trust) if results else None
        return {
            "name": name,
            "path": entry["path"],
            "parsed_artist": want_artist,
            "parsed_title": want_title,
            "n_results": len(results),
            "match": chosen,
        }
    except Exception as e:
        return {"name": name, "path": entry["path"],
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    # Throughput here is capped by AcoustID's rate limit, not by cores, so
    # workers only need to cover request latency (~1.2s at 2.5/s => ~3).
    # Scale gently with the host so a small VPS is not oversubscribed.
    ap.add_argument("-j", "--workers", type=int,
                    default=max(3, min(8, (os.cpu_count() or 4))))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    key = json.load(open(SECRETS))["acoustid_key"]
    fps = json.load(open(FP_CACHE))
    done = {} if args.force or not os.path.exists(OUT) else json.load(open(OUT))

    todo = [v for v in fps.values()
            if v.get("fingerprint") and v["path"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"  {len(done)} cached, {len(todo)} to look up, {args.workers} workers, "
          f"{RATE}/s cap")
    if not todo:
        print("  nothing to do\n")
        return

    limiter = RateLimiter(RATE)
    session = requests.Session()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(lookup, e, key, limiter, session) for e in todo]
        for i, f in enumerate(as_completed(futs), 1):
            res = f.result()
            done[res["path"]] = res
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"  {i}/{len(todo)}  {el:.0f}s  {el/i:.2f}s/track "
                      f"eta {(len(todo)-i)*el/i:.0f}s", flush=True)
                json.dump(done, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
                os.replace(OUT + ".tmp", OUT)

    json.dump(done, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)

    vals = list(done.values())
    matched = [v for v in vals if v.get("match")]
    withalb = [v for v in matched if v["match"].get("release")]
    errs = [v for v in vals if v.get("error")]
    print(f"\n  looked up {len(vals)}   matched {len(matched)} "
          f"({100*len(matched)/max(len(vals),1):.0f}%)   errors {len(errs)}")
    print(f"  of matched, {len(withalb)} carry an album/year "
          f"({100*len(withalb)/max(len(matched),1):.0f}%)")
    print(f"  wall {time.time()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
