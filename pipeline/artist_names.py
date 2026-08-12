#!/usr/bin/env python3
"""One spelling per artist, across the whole library.

The same performer arrives under several names -- "Joško Čagalj Jole" from a
MusicBrainz credit, "JOLE" from a YouTube title, "Jole" from a filename -- and
a player that groups by artist then shows three artists where there is one.

Two sources of truth, in order:

  1. config/artist_aliases.json, pinned by hand. Always wins.
  2. MusicBrainz artist MBIDs collected by cascade.py. When two spellings in
     the library resolve to the same MBID they are the same artist by
     definition, and the MBID's primary name is the canonical one.

Only the LEAD artist is rewritten. Featured performers live in their own tag
and in the title, so collapsing "Rasta; Buba Corelli" into "Rasta" would
destroy information rather than tidy it.

Usage:
  artist_names.py                # report what would change
  artist_names.py --apply        # write cache/artist_canon.json
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CONFIG = os.path.join(HERE, "config", "artist_aliases.json")
CASCADE = os.path.join(HERE, "cache", "cascade.json")
REVIEW = os.path.join(HERE, "cache", "review.json")
OUT = os.path.join(HERE, "cache", "artist_canon.json")


# Serbian/Macedonian Cyrillic -> Latin. Two-character sequences first, or
# "Љ" would come out as "L". This is the standard Gaj transliteration, which
# is what every Balkan release uses in Latin script.
_CYR = [("Љ", "Lj"), ("љ", "lj"), ("Њ", "Nj"), ("њ", "nj"), ("Џ", "Dž"),
        ("џ", "dž"), ("Ђ", "Đ"), ("ђ", "đ"), ("Ћ", "Ć"), ("ћ", "ć")]
_CYR += list(zip("АБВГДЕЖЗИЈКЛМНОПРСТУФХЦЧШЅЌЀ", [
    "A", "B", "V", "G", "D", "E", "Ž", "Z", "I", "J", "K", "L", "M", "N", "O",
    "P", "R", "S", "T", "U", "F", "H", "C", "Č", "Š", "Dz", "Ḱ", "E"]))
_CYR += list(zip("абвгдежзијклмнопрстуфхцчшѕќѐ", [
    "a", "b", "v", "g", "d", "e", "ž", "z", "i", "j", "k", "l", "m", "n", "o",
    "p", "r", "s", "t", "u", "f", "h", "c", "č", "š", "dz", "ḱ", "e"]))

# How a credit splits into individual artists. It lives here rather than in
# write_tags because both modules need the same answer: write_tags splits the
# field before canonicalising each part, and the rules built here have to be
# keyed the same way or they can never match what write_tags looks up.
ARTIST_SPLIT = re.compile(
    r"\s*;\s*|\s+&\s+|\s+[Xx]\s+|\s*,\s*(?=\S)"
    r"|\s+(?:[Ff]eat\.?|[Ff][Tt]\.?|[Ff]eaturing)\s+"
    r"|\s+i\s+")


def split_credit(credit):
    """-> every artist named in a credit string, in order.

    The counterpart to lead_of. A rule has to be keyed on one artist, because
    every caller splits the credit before looking anything up, so the
    generators that learn rules from the library have to split it too.
    """
    return [p.strip() for p in ARTIST_SPLIT.split(credit or "") if p.strip()]


def lead_of(credit):
    """-> the lead artist in a credit string.

    MusicBrainz gives one artist_id per recording and a credit that may name
    several people, so "mgk; Camila Cabello" carries mgk's MBID. Keying a rule
    on the whole credit produced rules like 'mgk; camila cabello' -> 'mgk',
    which are not spelling equivalences and never match, because every caller
    splits the credit first and looks up one artist at a time.
    """
    parts = split_credit(credit)
    return parts[0] if parts else (credit or "")


# Words a YouTube channel adds to an artist's own name. "Lucidious Music" is
# the channel Lucidious uploads to, not a second artist.
_CHANNEL_WORDS = re.compile(
    r"\s*\b(music|musik|official|officiel|vevo|tv|hd|records?|entertainment|"
    r"channel|topic|audio|productions?)\b\s*$", re.I)


def latin(s):
    """-> the name in Latin script. Cyrillic and Latin spellings of one artist
    are the same artist, and a phone that sorts A-Z buries the Cyrillic one."""
    if not s:
        return s
    for a, b in _CYR:
        if a in s:
            s = s.replace(a, b)
    return s


def is_cyrillic(s):
    return any("Ѐ" <= c <= "ӿ" for c in s or "")


def shouty(s):
    """-> True for a name written in capitals because a YouTube title was.

    Only ever used to break a tie between spellings we already know are the
    same artist, so XXXTENTACION and TTM -- who have no lower-case spelling
    anywhere in the library -- are never touched.
    """
    letters = [c for c in (s or "") if c.isalpha()]
    if len(letters) < 4:
        return False
    # A ratio, not "all upper": "RASTA x ALEN SAKIĆ" and "LUZERi" are shouting
    # too, and one stray lower-case letter should not exempt them.
    return sum(c.isupper() for c in letters) / len(letters) >= 0.8


def fold(s):
    """Case- and diacritic-insensitive key, so one alias entry covers them all."""
    s = latin(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    # Fold the channel suffix away too, so "Lucidious Music" and "Lucidious"
    # land on one key. Only ever strips a suffix, never a whole name.
    stripped = _CHANNEL_WORDS.sub("", s).strip()
    return stripped or s


def accents(s):
    """How many characters carry a diacritic."""
    return sum(1 for c in unicodedata.normalize("NFKD", s or "")
               if unicodedata.combining(c))


def load_pinned():
    if not os.path.exists(CONFIG):
        return {}
    data = json.load(open(CONFIG)).get("aliases") or {}
    return {fold(k): v for k, v in data.items()}


def from_musicbrainz():
    """-> {folded spelling: canonical name} for artists sharing an MBID.

    Two library spellings that resolve to the same MusicBrainz artist are the
    same artist; anything else is a guess and is left alone."""
    if not os.path.exists(CASCADE):
        return {}, {}
    cas = json.load(open(CASCADE))
    by_mbid = defaultdict(Counter)
    canon = {}
    for rec in cas.values():
        f = rec.get("facts") or {}
        mbid, artist = f.get("artist_id"), f.get("artist")
        if not (mbid and artist):
            continue
        # The MBID identifies the lead artist, so only the lead spelling is
        # evidence about that artist. The rest of the credit belongs to other
        # people and to their own MBIDs.
        by_mbid[mbid][lead_of(artist)] += 1
        if f.get("artist_canonical"):
            canon[mbid] = lead_of(f["artist_canonical"])
    out, groups = {}, {}
    for mbid, spellings in by_mbid.items():
        if len(spellings) < 2 and mbid not in canon:
            continue
        best = canon.get(mbid) or spellings.most_common(1)[0][0]
        if len(spellings) < 2 and fold(best) == fold(next(iter(spellings))):
            continue
        groups[best] = sorted(spellings)
        for s in spellings:
            if fold(s) != fold(best):
                out[fold(s)] = best
    return out, groups


def from_casing(rows, mapping):
    """-> {folded: preferred spelling} for names that differ only in case.

    "JOLE" off a YouTube title and "Jole" off a filename are one artist, but
    plain title-casing everything would wreck ANASTASIJA, TTM and
    XXXTENTACION, who really are capitalised that way. So the library's own
    most-used spelling decides, and MusicBrainz overrules it when it knows."""
    seen = defaultdict(Counter)
    for r in rows:
        # Split first, for the same reason from_musicbrainz has to: every
        # caller looks a credit up one artist at a time, so a key built from
        # the whole credit ("foo x bar") can never be found. Keying on the
        # whole string here would repeat the bug this module was fixed for.
        for a in split_credit(r.get("proposed_artist")):
            seen[fold(a)][a] += 1
    out = {}
    for key, spellings in seen.items():
        if len(spellings) < 2:
            continue
        if key in mapping:
            continue        # an alias rule already decides this one
        # Prefer the spelling that keeps its diacritics. Most of this library
        # is Croatian and Serbian, where "Milan Stankovic" is not a stylistic
        # variant of "Milan Stanković" -- it is the same name with characters
        # lost somewhere upstream, and frequency would pick the damaged one.
        # Latin script first: "Тоше Проески" and "Toše Proeski" are one artist,
        # and the Cyrillic spelling sorts away from every other Balkan name on
        # the phone. Then diacritics, then whichever the library uses most,
        # then the shorter name -- which drops the "Music" a channel adds.
        best = max(spellings, key=lambda s: (not is_cyrillic(s), not shouty(s),
                                             accents(s), spellings[s], -len(s)))
        out[key] = best
    return out


def _edits1(a, b):
    """-> True if one substitution/insertion/deletion apart."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    for j, c in enumerate(long):
        if i < len(short) and short[i] == c:
            i += 1
    return i == len(short)


def from_typos(rows, mapping):
    """-> {folded: preferred spelling} for misspelled names.

    "Demi Levato" came off an uploader's typo in a video title, and "Jean
    Carlos Canela" off a filename, and each became a second artist in the
    library beside the real one.

    Two ways in, both deliberately timid:

      * one character apart, where the correct spelling is well established
      * very similar AND sharing a distinctive word, where one spelling was
        confirmed (a link, a catalogue) and the other was only ever guessed

    The second is what catches "Jean Carlos" against "Jencarlos": two edits
    apart, one track each, so nothing about frequency alone could separate
    them -- but one of them is a filename guess at 0.60 and the other came
    from a link at 1.00.
    """
    seen, spell, best_conf = Counter(), {}, {}
    for r in rows:
        # Split for the same reason from_casing does: a rule keyed on a whole
        # credit is a rule nothing looks up.
        for a in split_credit(r.get("proposed_artist")):
            k = fold(a)
            seen[k] += 1
            spell.setdefault(k, Counter())[a] += 1
            best_conf[k] = max(best_conf.get(k, 0), r.get("confidence") or 0)

    def name_of(k):
        return spell[k].most_common(1)[0][0]

    out, keys = {}, sorted(seen)
    for rare in keys:
        if len(rare) < 8 or rare in mapping:
            continue
        for common in keys:
            if common == rare or fold(common) == fold(rare):
                continue
            if seen[rare] <= 1 and seen[common] >= 3 and _edits1(rare, common):
                out[rare] = name_of(common)
                break
            # Similar spelling, a shared distinctive word, and one side is
            # merely a guess while the other was confirmed.
            if best_conf[common] >= 0.9 > best_conf[rare] and \
                    SequenceMatcher(None, rare, common).ratio() >= 0.85 and \
                    _shares_rare_word(rare, common, seen):
                out[rare] = name_of(common)
                break
    return out


def _shares_rare_word(a, b, seen):
    """-> True if the two names share a word that is not common in the library.

    "Canela" appears in both spellings and almost nowhere else, which is what
    makes them the same person rather than two artists who happen to be 85%
    alike ("Milan Stankovic" and "Milan Stanojevic" are not)."""
    common_words = Counter()
    for k in seen:
        for w in k.split():
            common_words[w] += 1
    shared = {w for w in a.split() if len(w) >= 5} & {w for w in b.split() if len(w) >= 5}
    return any(common_words[w] <= 3 for w in shared)


def _resolve(mapping):
    """Follow each rule to where it actually ends.

    canonical() is a single lookup, so a rule whose target is itself a key
    leaves the name half-fixed. Pinning "mgk" -> "Machine Gun Kelly" against a
    MusicBrainz rule "machine gun kelly" -> "mgk" is exactly that: the two
    disagree about direction, and a single lookup turns "Machine Gun Kelly"
    into "mgk", the opposite of what was pinned.

    Following the chain settles it: the pinned end wins, because it is the only
    one a human asserted. A cycle with no pinned end keeps its first value
    rather than looping.

    Nothing is dropped on the way. A rule whose value equals its key still does
    work, because the key is folded and the name looked up is not: "30zona" ->
    "30zona" is what turns "30ZONA" into "30zona", and deleting it as a no-op
    put the shouting back.
    """
    out = {}
    for key, value in mapping.items():
        seen = {key}
        while fold(value) in mapping and fold(value) not in seen:
            seen.add(fold(value))
            value = mapping[fold(value)]
        out[key] = value
    return out


def build(rows=None):
    """-> (mapping, provenance). Pinned entries override MusicBrainz, which
    overrides a purely-cosmetic casing choice."""
    mb, groups = from_musicbrainz()
    pinned = load_pinned()
    mapping = dict(mb)
    # A pin also settles the direction of any rule pointing at it, so drop the
    # MusicBrainz rule that disagrees before it can win the chain.
    for k, v in pinned.items():
        mapping.pop(fold(v), None)
    mapping.update(pinned)
    why = {k: ("pinned" if k in pinned else "musicbrainz") for k in mapping}
    if rows is not None:
        for k, v in from_casing(rows, mapping).items():
            mapping[k], why[k] = v, "casing"
        for k, v in from_typos(rows, mapping).items():
            mapping[k], why[k] = v, "typo"
    mapping = _resolve(mapping)
    why = {k: why.get(k, "derived") for k in mapping}
    return mapping, why, groups


def canonical(artist, mapping):
    return mapping.get(fold(artist), artist)


def canonical_credit(credit, mapping):
    """-> the whole credit with every artist in it canonicalised separately.

    write_tags splits the credit and canonicalises each part, so a report that
    looks the whole credit up in one go describes something no stage does: it
    misses "mgk; Trippie Redd", whose lead is pinned, and it used to make the
    rules look like they collapsed a credit down to its lead artist.
    """
    parts = [p for p in ARTIST_SPLIT.split(credit or "") if p and p.strip()]
    if len(parts) < 2:
        return canonical(credit, mapping)
    out, rest = [], credit
    for p in parts:
        # Keep the separators the credit actually used.
        i = rest.index(p)
        out.append(rest[:i] + canonical(p.strip(), mapping))
        rest = rest[i + len(p):]
    return "".join(out) + rest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rows = json.load(open(REVIEW))
    mapping, why, groups = build(rows)
    hits = Counter()
    changed = []
    for r in rows:
        a = r.get("proposed_artist")
        if not a:
            continue
        c = canonical_credit(a, mapping)
        if c != a:
            hits[(a, c)] += 1
            changed.append((r["file"], a, c))

    print(f"\n  {len(mapping)} alias rules "
          f"({sum(1 for v in why.values() if v == 'pinned')} pinned, "
          f"{sum(1 for v in why.values() if v == 'musicbrainz')} from MusicBrainz)\n")
    if not hits:
        print("  nothing in the library uses a non-canonical spelling\n")
    for (a, c), n in hits.most_common():
        # A credit changes because one of the artists in it did, so the
        # provenance is that artist's, not the whole credit's -- looking the
        # credit up whole raised a KeyError on "RASTA x CORONA".
        parts = [p.strip() for p in ARTIST_SPLIT.split(a) if p and p.strip()]
        src = sorted({why[fold(p)] for p in parts if fold(p) in why})
        print(f"    {a[:34]:34} -> {c[:28]:28} {n:3} track(s)  "
              f"[{'+'.join(src) or 'derived'}]")

    if args.apply:
        json.dump({"mapping": mapping, "why": why, "groups": groups},
                  open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
        os.replace(OUT + ".tmp", OUT)
        print(f"\n  -> {OUT}  ({len(changed)} tracks affected)\n")
    else:
        print(f"\n  {len(changed)} tracks would change. Re-run with --apply.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
