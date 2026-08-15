#!/usr/bin/env python3
"""Pick ONE genre per track, most specific first.

Players show a single genre, so this is a ranking problem rather than a
collection problem. The order reflects how much each source actually knows
about this library:

  1. config/scenes.json -- hand-maintained, because no database holds these.
     Discogs styles Grse and Vojko V as Trap but has nothing for z++ or Zembo
     Latifa, and Last.fm has nothing for either. "Croatian trap" and "hrvatski
     trash" exist as scenes people book festivals around and as categories
     record blogs catalogue, and nowhere as machine-readable metadata.
  2. Last.fm scene tags -- ex-yu rock, novi val, serbian rap, turbofolk. Real
     community consensus where it exists, weighted so one person's private
     label does not become a genre.
  3. Discogs styles -- Trap, Punk, Europop, Ethno-pop. More specific than any
     genre field, and the one source that had Trap for Grse.
  4. Deezer genres -- Pop, Rock, Folk, Rap/Hip Hop. Broad, but present on
     nearly everything, so it is the floor rather than the goal.

Nothing here invents a genre. A track with no evidence gets no genre tag,
because a blank field is better than a wrong one.

Imported by the pipeline for genre_for(). Run directly, it reports what the
ranking would decide for each track in cache/review.json that has a proposed
artist, skipping the rest, and changes nothing.

Usage: scenes.py
"""
import json
import os
import re
import sys
import unicodedata
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import genres  # noqa: E402
from pipeline.genres import allow  # noqa: E402

CONFIG = os.path.join(HERE, "config", "scenes.json")
TAGS = os.path.join(HERE, "cache", "lastfm_tags.json")
ORIGIN = os.path.join(HERE, "cache", "artist_origin.json")

# Last.fm free text, collapsed onto the names you chose. Order matters: the
# first pattern that matches wins, so the specific ones come first.
_LASTFM = [
    (r"^ex[- ]?yu[- ]rock$", "Ex-YU"),
    (r"^ex[- ]?yu$", "Ex-YU"),
    # Novi val is the Yugoslav new wave, which is the Ex-YU era under another
    # name rather than a genre of its own. Mapped straight to the name that
    # survives the gate, so the table and genres.allow() cannot disagree.
    (r"^novi val$|^new wave$", "Ex-YU"),
    (r"^turbo[- ]?folk$|^pop[- ]?folk$", "Turbofolk"),
    (r"^serbian rap$|^serbian hip[- ]?hop$", "Serbian Rap"),
    (r"^croatian rap$|^croatian hip[- ]?hop$|^cro rap$", "Croatian Rap"),
    (r"^narodna$|^oriental$|^folk$", "Folk"),
    (r"^eurodance$", "Eurodance"),
    (r"^hardcore punk$|^punk rock$|^punk$", "Punk"),
    (r"^trap$|^cloud rap$|^experimental trap$", "Trap"),
]
_LASTFM = [(re.compile(p, re.I), name) for p, name in _LASTFM]

# Discogs styles worth promoting above a Deezer genre.
_DISCOGS = [
    (r"^trap$", "Trap"),
    (r"^punk$", "Punk"),
    (r"^turbo[- ]?folk$", "Turbofolk"),
    (r"^ethno[- ]?pop$", "Ethno-Pop"),
    (r"^europop$", "Europop"),
    (r"^eurodance$|^euro house$", "Eurodance"),
    (r"^new wave$", "Ex-YU"),
]
_DISCOGS = [(re.compile(p, re.I), name) for p, name in _DISCOGS]

# Tags that describe the listener, the chart or nothing at all.
_JUNK = re.compile(
    r"^(all|moja muzika|female vocalists|male vocalists|favorites?|seen live|"
    r"under \d+ listeners|garbage|zvezde granda|eurovision.*|my music|"
    r"various artists.*|awesome|beautiful|love|00s|90s|80s|10s"
    # Nationality is not a genre. It is already the Language crate, and
    # "serbian" as a genre tells you nothing "Serbian" did not. Shared with
    # genres._NOISE rather than spelled out twice: the two copies had drifted,
    # and this one was the fuller of them.
    r"|" + genres.PLACES + r")$", re.I)

MIN_TAG_WEIGHT = 10
# A tag also has to be substantial NEXT TO the artist's own top tag. Without
# this, "punk" sitting fifth on a band whose top tag is "acoustic" outranks it
# purely because "acoustic" is not in the mapping table.
MIN_TAG_SHARE = 0.25

# Deezer answers in the account's language, so a Croatian library gets Croatian
# genre names mixed in with English ones -- two spellings of one genre.
_DEEZER_EN = {
    "elektronička glazba": "Electronic",
    "alternativna glazba": "Alternative",
    "međunarodna pop glazba": "Pop",
    "filmovi/igrice": "Soundtrack",
    "rap/hip hop": "Hip-Hop",
    "techno/house": "Electronic",
    # "singer & songwriter" used to be translated to "Singer-Songwriter",
    # which described who made a record rather than what it sounds like and
    # reached exactly one track. genres.allow() drops it now, so the
    # translation would only have produced a name that dies one line later.
}


# Small words stay lower inside a name; everything else gets a capital, so
# "hip-hop" and "HIP HOP" both come out "Hip-Hop" rather than as two genres.
_SMALL = {"and", "of", "the", "n", "a"}
_ACRONYM = {"edm", "dj", "mc", "uk", "us", "ost", "nrg", "idm", "ebm"}


def titlecase(s):
    parts = re.split(r"([ \-/&])", (s or "").strip())
    out = []
    for i, p in enumerate(parts):
        if p in (" ", "-", "/", "&") or not p:
            out.append(p)
        elif i and p.lower() in _SMALL:
            out.append(p.lower())
        else:
            out.append(p.upper() if p.lower() in _ACRONYM
                       else p.capitalize())
    return "".join(out)


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9$+]+", " ", s).strip()


_CACHE = {}


def _load():
    if _CACHE:
        return _CACHE
    cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {"scenes": {}}
    scenes = []
    for name, spec in (cfg.get("scenes") or {}).items():
        # An entry is a name, or {name, years} where the act belongs to this
        # scene only for part of its career. Prljavo kazaliste were a Yugoslav
        # new wave band until 1991 and a Croatian pop-rock band after, so they
        # are listed in two scenes with the years deciding which applies.
        members = {}
        for a in spec.get("artists") or []:
            if isinstance(a, dict):
                members[fold(a.get("name"))] = a.get("years") or spec.get("years")
            else:
                members[fold(a)] = spec.get("years")
        scenes.append((name, members))
    _CACHE["scenes"] = scenes
    tags = json.load(open(TAGS)) if os.path.exists(TAGS) else {}
    # Folded keys throughout. split_credits yields the lead exactly as it was
    # credited, so the same artist arrives as "Rasta" from one file and
    # "RASTA" from another -- and a case-sensitive lookup missed the guard
    # that keeps a Belarusian metal band's tags off Serbian rap.
    _CACHE["artist_tags"] = {fold(k): v
                             for k, v in (tags.get("artist") or {}).items()}
    _CACHE["track_tags"] = {fold(k): v
                            for k, v in (tags.get("track") or {}).items()}
    org = json.load(open(ORIGIN)) if os.path.exists(ORIGIN) else {}
    _CACHE["origin"] = {fold(k): v for k, v in org.items()}
    return _CACHE


def scenes_for(artist, year=None):
    """-> every hand-curated scene this artist belongs to.

    A list, not one answer: Prljavo kazaliste are genuinely both Ex-YU Rock
    and Croatian Trash, and picking one would misrepresent half the catalogue.
    The first is the primary -- it is what a player showing a single genre
    will display -- and the rest still reach the file and the playlists.
    """
    key, out = fold(artist), []
    for name, members in _load()["scenes"]:
        if key not in members:
            continue
        years = members[key]
        if years and year:
            try:
                if not (years[0] <= int(str(year)[:4]) <= years[1]):
                    continue
            except (ValueError, TypeError):
                pass
        out.append(name)
    return out


def scene_for(artist, year=None):
    """-> the primary scene, or None."""
    got = scenes_for(artist, year)
    return got[0] if got else None


def _from_tags(tags, table):
    """-> the first mapped name among tags heavy enough to be real."""
    top = max((t.get("count", 0) for t in tags or [] if isinstance(t, dict)),
              default=0)
    floor = max(MIN_TAG_WEIGHT, top * MIN_TAG_SHARE)
    for t in tags or []:
        name = t.get("name") if isinstance(t, dict) else t
        weight = t.get("count", 999) if isinstance(t, dict) else 999
        if not name or weight < floor or _JUNK.match(name.strip()):
            continue
        for rx, mapped in table:
            if rx.match(name.strip()):
                return mapped
    return None


MIN_SHARED_ARTISTS = 3


def _shared_tags():
    """-> tags used on at least MIN_SHARED_ARTISTS artists in this library."""
    c = _load()
    if "shared" in c:
        return c["shared"]
    seen = {}
    for tags in c["artist_tags"].values():
        for t in tags or []:
            nm = (t.get("name") or "").strip().lower()
            if nm and t.get("count", 0) >= MIN_TAG_WEIGHT:
                seen[nm] = seen.get(nm, 0) + 1
    c["shared"] = {k for k, n in seen.items() if n >= MIN_SHARED_ARTISTS}
    return c["shared"]


def _deezer(name):
    """-> the whitelisted genre a Deezer answer means, or None.

    One helper because there are two Deezer paths: the ordinary floor at the
    bottom of genre_for, and the ISRC branch above it. The ISRC branch used
    to skip the Croatian-to-English table entirely, so ten tracks whose genre
    Deezer gave as "Rap/Hip Hop" arrived untranslated and lost their genre.
    """
    if not name:
        return None
    return allow(_DEEZER_EN.get(name.strip().lower(), name))


def scene_names():
    """-> every scene name config/scenes.json defines.

    export.py needs these to tell a hand-curated crate from a generic genre,
    and reading them from the config means adding a scene does not also mean
    editing a second list that would otherwise silently disagree.
    """
    return [name for name, _members in _load()["scenes"]]


def genre_for(artist, title=None, year=None, discogs_styles=None,
              deezer_genre=None):
    """-> (genre, why) picking the most specific evidence available."""
    scene = scenes_for(artist, year)
    if scene:
        # Not gated: scenes.json is hand-written, so it is already a
        # whitelist. Gating it would mean a scene you add here vanishes
        # unless you also remember to add it to genres.GENRES.
        return scene[0], "scene list"

    c = _load()
    # If origin.py had to fall back to the ISRC for this artist, it did so
    # precisely because Last.fm's answer named no Balkan country -- the name
    # landed on someone else. "Rasta" returns a Belarusian melodic death metal
    # band, and trusting its tags here put "melodic death metal" on Serbian
    # rap. Having already judged those tags to be about a different artist,
    # they cannot be used for genre either.
    if (c["origin"].get(fold(artist)) or {}).get("why") == "isrc":
        dz = _deezer(deezer_genre)
        return (dz, "deezer") if dz else (None, None)

    a_tags = c["artist_tags"].get(fold(artist)) or []
    t_tags = c["track_tags"].get(fold(f"{artist}|{title}")) or []
    # Track tags first: a rapper's acoustic ballad is tagged as itself.
    # Every answer below goes through genres.allow(), and a source whose
    # answer the gate drops is treated as having had no answer, so the next
    # source still gets its turn rather than the track losing its genre.
    for tags, why in ((t_tags, "lastfm track"), (a_tags, "lastfm artist")):
        got = allow(_from_tags(tags, _LASTFM))
        if got:
            return got, why

    got = allow(_from_tags(discogs_styles or [], _DISCOGS))
    if got:
        return got, "discogs style"

    if deezer_genre:
        # This is the line that let "Dance" onto 85 files: whatever Deezer
        # answered went straight to a TCON frame. Now it has to be a name we
        # chose, or it is not a genre.
        got = _deezer(deezer_genre)
        if got:
            return got, "deezer"
    # Nothing specific: fall back to the heaviest Last.fm tag -- but only one
    # that several artists in this library share. A blocklist cannot keep up
    # with free text, and one-off tags produced genres called "5 Stars",
    # "Brcko", "Gazda Paja" and "Glee". Appearing across several artists is
    # what separates a genre from someone's private label, without needing to
    # enumerate either.
    shared = _shared_tags()
    for tags in (t_tags, a_tags):
        for t in tags:
            nm = t.get("name") if isinstance(t, dict) else t
            if nm and nm.strip().lower() not in shared:
                continue
            name = t.get("name") if isinstance(t, dict) else t
            w = t.get("count", 0) if isinstance(t, dict) else 0
            if name and w >= 50 and not _JUNK.match(name.strip()):
                # The other source that used to write free text. "5 Stars",
                # "Gazda Paja" and "Glee" all arrived through here, and the
                # shared-tag test alone never caught them.
                got = allow(titlecase(name))
                if got:
                    return got, "lastfm broad"
    return None, None


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()

    from pipeline import artist_names
    from pipeline.write_tags import split_credits
    rows = json.load(open(os.path.join(HERE, "cache", "review.json")))
    cas_path = os.path.join(HERE, "cache", "cascade.json")
    cas = json.load(open(cas_path)) if os.path.exists(cas_path) else {}
    mapping, _w, _g = artist_names.build(rows)

    counts, why_counts, unknown = Counter(), Counter(), Counter()
    for r in rows:
        a, t = r.get("proposed_artist"), r.get("proposed_title")
        if not a:
            continue
        lead = artist_names.canonical(split_credits(a, t or "")[0], mapping)
        facts = (cas.get(r["path"]) or {}).get("facts") or {}
        dz = facts.get("genres")
        dz = dz[0] if isinstance(dz, list) and dz else (dz if isinstance(dz, str) else None)
        g, why = genre_for(lead, t, r.get("proposed_year"), None, dz)
        counts[g or "(none)"] += 1
        why_counts[why or "(none)"] += 1
        if not g:
            unknown[lead] += 1

    print(f"\n  genre for {sum(counts.values())} tracks\n")
    for g, n in counts.most_common(30):
        print(f"    {n:5}  {g}")
    print("\n  decided by:")
    for w, n in why_counts.most_common():
        print(f"    {n:5}  {w}")
    if unknown:
        print(f"\n  {sum(unknown.values())} tracks with no genre, "
              f"top artists: {', '.join(a for a, _ in unknown.most_common(8))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
