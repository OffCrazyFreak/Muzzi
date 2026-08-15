#!/usr/bin/env python3
"""Which country an artist is from, so "Balkan" can be split into real scenes.

Whisper and langdetect cannot separate Croatian from Serbian, and they are not
wrong to refuse: BCMS is one pluricentric language and the difference between
"lijepo" and "lepo" is not something a speech model reports. Every Balkan track
in this library is tagged `hr` for that reason, which makes one enormous
"Language - Balkan" playlist and no way to browse a scene.

Origin is a different question from language, and it is answerable:

  1. Last.fm artist tags. The community tags Balkan artists by nationality --
     "croatian", "serbian", "Bosnian", "Beograd", "Sarajevo" -- and by scene,
     "ex-yu", "ex-yu rock", "serbian rap", "novi val".
  2. ISRC registrant country, which we already hold for 1590 tracks. It is the
     LABEL's country rather than the artist's, so it is a fallback and a
     cross-check, never the sole answer.

Last.fm is keyed by name, so it collides: "Rasta" returns a Belarusian melodic
death metal band, and our own MusicBrainz id for that artist points at the same
wrong band. So a Last.fm answer is only believed when it is Balkan at all --
otherwise the name has clearly landed on someone else and ISRC decides.

Writes cache/artist_origin.json. export.py reads it to split the Balkan crate.

Usage:
  origin.py                 # report
  origin.py --apply
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import artist_names, lastfm_tags  # noqa: E402
from pipeline.write_tags import split_credits  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
CASCADE = os.path.join(HERE, "cache", "cascade.json")
SECRETS = os.path.join(HERE, "config", "secrets.json")
OUT = os.path.join(HERE, "cache", "artist_origin.json")

# Only the ex-Yugoslav countries. Everything else stays "Balkan" or keeps its
# own language crate, because splitting further is not what this is for.
COUNTRIES = {
    "hr": "Croatian", "rs": "Serbian", "ba": "Bosnian",
    "si": "Slovenian", "mk": "Macedonian", "me": "Montenegrin",
}

# Last.fm tags that name a country, including cities and adjectives, since the
# community tags "Beograd" and "Sarajevo" as often as "serbian".
_TAGS = {
    "hr": ("croatian", "croatia", "hrvatska", "hrvatski", "zagreb", "split",
           "dalmatian", "dalmacija", "cro rap", "croatian rock"),
    "rs": ("serbian", "serbia", "srbija", "srpski", "beograd", "belgrade",
           "novi sad", "serbian rap", "serbian pop"),
    "ba": ("bosnian", "bosnia", "bosna", "bosanski", "sarajevo", "bosansko",
           "bosnia and herzegovina", "bosnia-herzegovina", "herzegovina"),
    "si": ("slovenian", "slovenia", "slovenija", "ljubljana"),
    "mk": ("macedonian", "macedonia", "makedonija", "skopje"),
    "me": ("montenegrin", "montenegro", "crna gora", "podgorica"),
}
# ISRC prefixes. CS is the retired Serbia-and-Montenegro code and YU the
# retired Yugoslav one; both appear on reissues of older material.
_ISRC = {"HR": "hr", "RS": "rs", "BA": "ba", "SI": "si", "MK": "mk",
         "ME": "me", "CS": "rs", "YU": "rs"}

# How many of an artist's tags are consulted. Last.fm returns them by weight,
# so this is the top of the list rather than an arbitrary slice.
TOP_TAGS = 15


def artist_tags(name, key, session, cache):
    """-> [tag, ...] lower-cased, or [].

    Reads `cache/lastfm_tags.json`, the same file lastfm_tags.py fills, rather
    than a dict that lived for one run. Both stages ask Last.fm the same
    question (`artist.gettoptags`, autocorrect on) about the same artists, and
    this one asked first and threw the answers away: measured, 324 of the 690
    seconds of a fully cached run were origin re-fetching 727 artists that the
    next stage already held 240 of. run.py now runs lastfm_tags first, so on a
    warm library this loop makes no requests at all.
    """
    return [t["name"] for t in lastfm_tags.tags_for(
        session, key, "artist.gettoptags", {"artist": name},
        cache, name)][:TOP_TAGS]


def country_from_tags(tags):
    """-> country code, or None when the tags name no Balkan country.

    None is meaningful: it is how the Belarusian band that also calls itself
    Rasta is rejected instead of being recorded as "not Croatian".
    """
    hits = Counter()
    for cc, words in _TAGS.items():
        for w in words:
            if w in tags:
                hits[cc] += 1
    return hits.most_common(1)[0][0] if hits else None


def scene_tags(tags):
    """-> the regional genre tags worth keeping, e.g. ex-yu rock, serbian rap.

    Free-text tags, so the same scene arrives spelled several ways: "ex-yu
    rock" and "ex-yu-rock" made two crates of one thing. Plain "balkan" is
    dropped -- it says nothing the Language split does not already say, and it
    was the largest crate by far.
    """
    keep = []
    for t in tags:
        t = t.replace("-", " ").replace("_", " ")
        t = " ".join(t.split())
        if t == "balkan":
            continue
        if t.startswith("ex yu") or t == "novi val" or (
                t.endswith(" rap") and
                any(c in t for c in ("serbian", "croatian", "bosnian", "cro"))):
            keep.append(t.replace("ex yu", "ex-yu"))
    return sorted(set(keep))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    rows = json.load(open(REVIEW))
    cas = json.load(open(CASCADE)) if os.path.exists(CASCADE) else {}
    mapping, _why, _g = artist_names.build(rows)

    # ISRC country per artist, from the tracks we already resolved.
    isrc_by_artist = defaultdict(Counter)
    for p, v in cas.items():
        isrc = (v.get("facts") or {}).get("isrc")
        artist = (v.get("facts") or {}).get("artist")
        if isrc and artist and len(isrc) >= 2:
            cc = _ISRC.get(isrc[:2].upper())
            if cc:
                isrc_by_artist[artist_names.canonical(artist, mapping)][cc] += 1

    # Only ask about artists that might be Balkan at all: those with an ex-YU
    # ISRC, or whose tracks were detected as a Balkan language.
    # Every lead artist, not a pre-filtered subset. Narrowing by the `balkan`
    # flag or by an ex-YU ISRC missed Buba Corelli entirely -- his releases
    # carry an Austrian ISRC and none of his tracks were flagged -- while
    # Last.fm calls him Bosnian without hesitation. Asking about all 727 costs
    # under three minutes, and the Balkan-tags-only rule already discards
    # every answer about an artist who is not from there.
    candidates = set(isrc_by_artist)
    for r in rows:
        a = r.get("proposed_artist")
        if a:
            lead = split_credits(a, r.get("proposed_title") or "")[0]
            candidates.add(artist_names.canonical(lead, mapping))
    candidates = sorted(c for c in candidates if c)
    if args.limit:
        candidates = candidates[: args.limit]

    key = json.load(open(SECRETS)).get("lastfm_api_key")
    session = requests.Session()
    # The raw Last.fm answers are a cache, not this stage's verdict, so they
    # are kept whether or not --apply was given. artist_origin.json, which is
    # the verdict, still only moves under --apply.
    tags_cache = lastfm_tags.load_cache()
    before = len(tags_cache["artist"])
    out, stats = {}, Counter()
    print(f"\n  {len(candidates)} candidate artists\n")
    for name in candidates:
        tags = artist_tags(name, key, session, tags_cache["artist"]) if key else []
        by_tag = country_from_tags(tags)
        by_isrc = (isrc_by_artist[name].most_common(1)[0][0]
                   if isrc_by_artist.get(name) else None)
        cc, why = None, None
        if by_tag:
            cc, why = by_tag, "lastfm"
            if by_isrc and by_isrc != by_tag:
                why = "lastfm (isrc disagrees)"
        elif by_isrc:
            # Either the artist is not Balkan, or Last.fm has the wrong
            # namesake. The label's country is the better evidence we have.
            cc, why = by_isrc, "isrc"
        if cc:
            out[name] = {"country": cc, "label": COUNTRIES[cc], "why": why,
                         "scene": scene_tags(tags)}
            stats[f"{COUNTRIES[cc]} ({why.split()[0]})"] += 1
        else:
            stats["no origin found"] += 1

    for k, n in stats.most_common():
        print(f"    {k:34} {n}")
    fetched = len(tags_cache["artist"]) - before
    print(f"\n    {fetched} artists fetched from Last.fm, "
          f"{len(candidates) - fetched} already cached")
    if fetched:
        lastfm_tags.save_cache(tags_cache)
    if args.apply:
        json.dump(out, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
        os.replace(OUT + ".tmp", OUT)
        print(f"\n  -> {OUT}  ({len(out)} artists)\n")
    else:
        print(f"\n  {len(out)} artists resolved. Re-run with --apply.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
