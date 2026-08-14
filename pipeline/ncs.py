#!/usr/bin/env python3
"""The NoCopyrightSounds catalogue, for the genre it publishes and nothing else.

111 tracks here sit in `NCS Beat` and `NCS Chill`. Their identity is not the
problem: all 111 are already auto-accepted at a median confidence of 1.0 and
none of them is in review. Their genre is. Measured against the rest of the
library:

    NCS folders       52 of 111 have any genre at all   (47%)
    everything else  1306 of 1612                       (81%)

and what the 52 do have is partly Last.fm noise: `Germany, Electronic,
Dubstep` and `Electro, Electronic, Norway` put countries in a genre field.
NCS publishes exactly one genre per release, which is the thing missing.

What this may and may not decide
--------------------------------

**It never sets an artist or a title.** #60 is explicit that an NCS-looking
folder must not prove an NCS release, and the folder is what triggers the
lookup, so letting the answer name the track would be the folder proving
itself through one indirection. Only `genre` and the Regular/Instrumental
version marker are recorded, and a match needs the artist AND the title to
agree independently before even those are taken.

The folder decides who is asked, never what the answer is.

How it is fetched
-----------------

There is no API. `ncs.io` serves HTML and the only search form (`?q=`) is a
302 to the front page, so per-track lookup is not available at any price. What
the listing pages do carry is a complete record per track in data attributes:

    data-artistraw="Janji"  data-track="Horizon"  data-genre="House"
    data-tid="<uuid>"       data-versions="Regular"

So the whole catalogue is paged once into `cache/ncs.json` and every match
after that is local. That is the opposite of the usual trade here and it is
the right way round: one bulk fetch costs about 100 requests once, where
per-track lookup would cost 111 and could not be cached against the next run.

Usage:
  ncs.py --refresh          # page the catalogue into cache/ncs.json
  ncs.py --match "Janji" "Horizon"
  ncs.py                    # what the cache holds, and what it covers here
"""
import argparse
import html
import json
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.useragent import UA  # noqa: E402

OUT = os.path.join(HERE, "cache", "ncs.json")
BASE = "https://ncs.io/?display=&page={page}"

# Every field comes off one anchor, in this order, so one regex reads a whole
# record and a partial one is simply not matched. Fragile by nature: this is
# scraped markup, not a contract, which is exactly why `probe()` checks that it
# still parses rather than that the page still returns 200.
RECORD = re.compile(
    r'data-artistraw="([^"]*)"[^>]*?data-track="([^"]*)"[^>]*?'
    r'data-tid="([^"]*)"[^>]*?data-versions="([^"]*)"[^>]*?'
    r'data-genre="([^"]*)"')

# Stop after this many pages that add nothing. The listing repeats a featured
# block on every page, so "this page had records" is not an end condition and
# "this page had no NEW records" is.
DRY_PAGES = 3
PAGE_CAP = 200
PAUSE = 0.4

# What marks a track as NCS's to look up. The folder, and only as a trigger.
FOLDER = re.compile(r"\bNCS\b", re.I)


# What a YouTube title drags in behind the song's name. Measured on the misses:
# 24 of this library's NCS titles are the whole video title, genre bar and all,
# as in "OMG | House | NCS - Copyright Free Music".
_BAR_TAIL = re.compile(r"\s*\|.*$")
# A feature credit NCS keeps in the artist and this library keeps in the title.
_FEAT = re.compile(r"\s*[\(\[]?\b(?:feat|ft)\b\.?\s.*$", re.I)
# How both sides spell "and". NCS uses a comma, downloads use every other form.
_SPLIT = re.compile(r"\s*(?:,|;|&|\bx\b|\bvs\.?\b|\bfeat\b\.?|\bft\b\.?|/)\s*",
                    re.I)


def skeleton(s):
    """-> `s` reduced to ASCII letters and digits, casefolded.

    The same comparison `confidence` uses, and for the same reason: these
    filenames lost their diacritics somewhere between YouTube and here, and
    "Kedo Rebelle" has to match "Kédo Rebelle".
    """
    return "".join(c for c in (s or "") if c.isascii() and c.isalnum()).casefold()


def title_forms(title):
    """-> the skeletons this title might be filed under, widest last.

    Three spellings of one name, because the same track is "OMG" on NCS and
    "OMG | House | NCS - Copyright Free Music" here, and "Blinded" there
    against "Blinded (feat. Kosta & Theo Hoarau)" here. NCS carries its
    features in the artist, this library carries them in the title, and
    neither is wrong.
    """
    out = []
    for form in (title, _BAR_TAIL.sub("", title or ""),
                 _FEAT.sub("", _BAR_TAIL.sub("", title or ""))):
        k = skeleton(form)
        if k and k not in out:
            out.append(k)
    return out


def artist_set(artist):
    """-> the individual names in a credit, as skeletons.

    Compared as a set because the order is not information: this library has
    "Curbi; Ash O'Connor" where NCS has "Ash O'Connor, Curbi", and they are one
    record. Kept as a set rather than a string so that a subset still counts,
    since "Jim Yosef" here is "Jim Yosef, Anna Yvette" there.
    """
    return {k for k in (skeleton(p) for p in _SPLIT.split(artist or "")) if k}


def _page(session, n):
    """-> [(artist, title, tid, versions, genre), ...] from one listing page."""
    r = session.get(BASE.format(page=n), headers=UA, timeout=30)
    if r.status_code != 200:
        return None
    return RECORD.findall(r.text)


def refresh(session=None, cap=PAGE_CAP, verbose=True):
    """Page the whole catalogue into `cache/ncs.json`. -> {tid: record}.

    Merged into whatever is already cached rather than replacing it. A run that
    dies halfway, or a page that 404s on the day, then costs the pages it did
    not reach instead of the whole catalogue, and a track NCS has since taken
    down keeps the genre it had.
    """
    s = session or requests.Session()
    cat = load()
    page, dry = 1, 0
    while page <= cap and dry < DRY_PAGES:
        got = _page(s, page)
        if got is None:
            dry += 1
            page += 1
            continue
        new = 0
        for artist, title, tid, versions, genre in got:
            if tid and tid not in cat:
                cat[tid] = {"artist": html.unescape(artist),
                            "title": html.unescape(title),
                            "genre": html.unescape(genre),
                            "versions": versions}
                new += 1
        dry = dry + 1 if not new else 0
        if verbose and page % 20 == 0:
            print(f"    page {page}: {len(cat)} tracks", flush=True)
        page += 1
        time.sleep(PAUSE)
    _save(cat, load_matched())
    return cat


def load(path=None):
    """-> {tid: record}, or {} when nothing has been fetched yet.

    The file holds `{"catalogue": ..., "matched": ...}`. A bare mapping of
    records is accepted too, because that was the shape while this was being
    measured and reading it costs one `in` test.
    """
    try:
        with open(path or OUT, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(got, dict) and "catalogue" in got:
        return got.get("catalogue") or {}
    return got if isinstance(got, dict) else {}


def _index(cat):
    """-> {title skeleton: [record, ...]}, each record carrying its artist set.

    Keyed on the title alone and narrowed by artist at lookup, rather than on
    the pair. The pair cannot be a key here because the two sides spell a
    multi-artist credit differently and neither spelling is canonical.

    Filed under every width of its own title, not only its exact one, because
    the feature credit moves in both directions: this library has "Cartoon;
    Daniel Levi" and "On & On" where NCS has "Cartoon" and "On & On (feat.
    Daniel Levi)". Stripping only our side found neither.
    """
    out = {}
    for rec in cat.values():
        rec = dict(rec, _artists=artist_set(rec["artist"]))
        for key in title_forms(rec["title"]):
            out.setdefault(key, []).append(rec)
    return out


def match(artist, title, cat=None, index=None):
    """-> the NCS record for this track, or None.

    Both halves have to agree, and that is the rule keeping the folder from
    deciding the answer: a file in `NCS Beat` that NCS does not have gets
    nothing. A title alone is not enough, because "Fade" and "Reflections" are
    each several different NCS tracks, and an artist alone is worth less
    still, since these artists release dozens each.

    Agreement is not equality, in either half. The title is tried at three
    widths, because this library files a track as the whole YouTube video
    title. The artist is compared as a set and matches when either side
    contains the other, because NCS credits a feature in the artist where the
    filename credits it in the title, so "Jim Yosef" here is "Jim Yosef, Anna
    Yvette" there and they are one track.

    A remix is not the track it remixes, and NCS lists both: five records share
    the title "On & On" once the feature credit is off, of which four are
    remixes. They are separated with `webmatch.version_mismatch`, the same
    check the rest of the pipeline uses to refuse a "(Balkanik Remix)" match
    for a plain filename, rather than a second opinion about version words
    invented here.

    Anything still ambiguous returns None rather than a guess. Two survivors
    is usually the Regular/Instrumental pair, and picking the wrong one would
    put the wrong version marker on the file, which is a hard constraint
    elsewhere in this pipeline.
    """
    from pipeline.webmatch import version_mismatch
    idx = index if index is not None else _index(cat if cat is not None
                                                 else load())
    mine = artist_set(artist)
    if not mine:
        return None
    for key in title_forms(title):
        got = [r for r in idx.get(key, ())
               if r["_artists"] <= mine or mine <= r["_artists"]]
        got = [r for r in got if not version_mismatch(title, r["title"])]
        # Deduplicate: a record filed under two widths of its own title can be
        # reached twice by one lookup, and two references to one track are not
        # an ambiguity.
        seen, uniq = set(), []
        for r in got:
            if id(r) not in seen:
                seen.add(id(r))
                uniq.append(r)
        if len(uniq) == 1:
            return {k: v for k, v in uniq[0].items() if k != "_artists"}
        if uniq:
            return None                 # several, and no way to choose here
    return None


def is_ncs(path):
    """-> whether this file's folder says to ask NCS about it.

    The trigger, and nothing more. `#60`: an NCS-looking folder may suggest
    provenance and must not prove a release, so this decides only who gets
    looked up.
    """
    return bool(FOLDER.search(os.path.basename(os.path.dirname(path or ""))))


def probe(session):
    """-> (ok, detail). The health canary for this source.

    Asks a question whose answer is already known: page 1 must parse into
    records with a genre on them. Checking the status code alone would pass a
    redesigned page that returns a cheerful 200 of markup this cannot read,
    which is the failure this source is most likely to have, because it is
    scraped rather than published.
    """
    try:
        got = _page(session, 1)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"
    if got is None:
        return False, "page 1 did not return 200"
    if not got:
        return False, "page 1 parsed to no records: the markup has changed"
    if not any(g[4] for g in got):
        return False, "records carry no genre: the markup has changed"
    return True, f"{len(got)} records on page 1"


REVIEW = os.path.join(HERE, "cache", "review.json")


def match_library(rows=None, cat=None, conn=None):
    """-> {path: record} for every NCS-foldered track the catalogue has.

    The folder chooses who is asked. The catalogue decides whether there is an
    answer, and 18 of these 111 get none, which is the property worth having:
    a file in `NCS Beat` that NCS does not list stays exactly as it was.

    Also written to the evidence store when a connection is given, under the
    `ncs` family, so a genre that came from here is attributable later rather
    than appearing in the tag from nowhere.
    """
    if rows is None:
        with open(REVIEW, encoding="utf-8") as fh:
            rows = json.load(fh)
    idx = _index(cat if cat is not None else load())
    out = {}
    for r in rows:
        path = r.get("path")
        if not path or not is_ncs(path):
            continue
        got = match(r.get("proposed_artist"), r.get("proposed_title"),
                    index=idx)
        if got:
            out[path] = got
    if conn is not None:
        from pipeline import evidence
        for path, rec in out.items():
            evidence.record(conn, path, "genre", "ncs",
                            f"ncs:{rec['artist']}|{rec['title']}",
                            evidence.FOUND, value=rec["genre"])
        conn.commit()
    return out


def genre_for(path, matched=None):
    """-> the NCS genre for one file, or None."""
    rec = (matched if matched is not None else load_matched()).get(path)
    return (rec or {}).get("genre")


def load_matched(path=None):
    """-> {path: record} as last written by --apply."""
    try:
        with open(path or OUT, encoding="utf-8") as fh:
            return json.load(fh).get("matched") or {}
    except (OSError, ValueError):
        return {}


def _save(cat, matched):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"catalogue": cat, "matched": matched}, fh,
                  ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="page the catalogue from ncs.io into cache/ncs.json")
    ap.add_argument("--apply", action="store_true",
                    help="match the library against the cached catalogue")
    ap.add_argument("--no-evidence", dest="evidence", action="store_false",
                    help="do not record what NCS said into the store")
    ap.add_argument("--match", nargs=2, metavar=("ARTIST", "TITLE"))
    args = ap.parse_args()

    cat = load()
    if args.refresh:
        print("\n  paging the NCS catalogue")
        cat = refresh()
        print(f"    {len(cat)} tracks")

    if not cat:
        print("\n  nothing cached yet. Run ncs.py --refresh\n")
        return 1

    if args.match:
        print(f"\n  {match(*args.match, cat=cat) or 'no match'}\n")
        return 0

    if args.refresh or args.apply:
        conn = None
        if args.evidence:
            from pipeline import evidence
            conn = evidence.connect()
        matched = match_library(cat=cat, conn=conn)
        asked = sum(1 for r in json.load(open(REVIEW)) if is_ncs(r.get("path")))
        _save(cat, matched)
        print(f"    {len(matched)} of {asked} NCS-foldered tracks matched")
        print(f"    -> {OUT}\n")
        return 0

    genres = {}
    for rec in cat.values():
        genres[rec["genre"]] = genres.get(rec["genre"], 0) + 1
    matched = load_matched()
    print(f"\n  {len(cat)} NCS tracks cached, {len(genres)} distinct genres, "
          f"{len(matched)} files matched")
    for g, n in sorted(genres.items(), key=lambda x: -x[1])[:12]:
        print(f"    {n:5}  {g}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
