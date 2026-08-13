#!/usr/bin/env python3
"""Report artist names that break the rules, one section per rule.

Reports only. Nothing here writes to the library, to cache/ or to config/.

Two surfaces, because they disagree. --review audits what the pipeline would
write next (proposed_artist with artist_canon.json applied); --tags audits what
the phone shows now, read from the files in output/_all. output/_all is only rewritten
when write_tags runs, so a name fixed in the proposal can still be wrong on the
phone, and a name broken only on disk never appears in the proposal at all.

Rules 4 and 5 report nothing both when they work and when they are broken, so
--selftest exercises them against names known to break and known not to.

Usage:
  audit_artists.py --review cache/review.json --canon cache/artist_canon.json
  audit_artists.py --tags output/_all
  audit_artists.py --selftest
"""
import argparse
import json
import re
import sys
import os
import unicodedata
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import artist_names  # noqa: E402

# Rule 3: three or more capitals in a row. Two is a normal initialism ("DJ"),
# three is a name that came off a shouting YouTube title.
CAPS_RUN = re.compile(r"[A-ZČĆŽŠĐ]{3,}")

QUOTES = "\"'‘’“”„‟«»‹›`´"
BRACKETS = "()[]{}<>（）【】"

FEAT = re.compile(r"(?<![a-z])(feat\.?|ft\.?|featuring|prati|gost(uje)?)(?![a-z])",
                  re.I)
# "mix" and "edit" alone are not enough: "Little Mix" is a band and "Edit"
# appears in no artist name here. Only unambiguous remix-credit words.
REMIX = re.compile(r"(?<![a-z])(remix|rmx|bootleg|mashup|re-?edit|flip|vip mix)"
                   r"(?![a-z])", re.I)

# Rule 9: words that join two artists into one string. "i" and "and" are whole
# words only, or every name containing the letter i matches.
JOINERS = ["and", "&", "i", "und", "en", "e", "y", "zajedno", "with", "vs",
           "vs.", "versus", "x", "X", "+", ",", ";", "/", "meets", "duet",
           "pres", "pres.", "presents"]
JOINER_RE = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(w) for w in JOINERS) + r")(?![\w])")

# Rule 6: anything outside letters, digits, space and the punctuation a real
# name uses. Catches Axwell /\ Ingrosso, A$AP, P!nk, Ke$ha, ¥$. The ASCII
# hyphen is allowed and U+2010 is not, deliberately: "Ne‐Yo" with a typographic
# hyphen is a different string to "Ne-Yo" and becomes a second artist.
ALLOWED_PUNCT = set(" .-'&,!$")

# ";" joins several artists into one field. It is the library's separator, not
# part of a name, so every rule runs against the components rather than the
# joined string -- otherwise "Calvin Harris; Sia" reads as a symbol violation
# and the 167 real findings are buried under 150 correct ones.
SEPARATOR = ";"


def components(name):
    """-> the individual artists in one artist field.

    Splits the way write_tags does, not on ";" alone. A credit joined by "&",
    "x", "i" or a feature marker is several artists to the writer, so an audit
    that keeps it whole reports violations the writer will never produce, and
    misses the ones it will.
    """
    return artist_names.split_credit(name)

# Rule 4: the transliterations that turn one name into two. English spelling
# on the left, Croatian on the right.
# Longest digraph first: applied in list order, ("ch", "c") would reduce
# "tch" to "tc" and ("tch", "c") could then never fire, so "Mitch" keyed to
# "mitc" and a Mitch/Mic pair went unreported.
TRANSLIT = [("x", "ks"), ("y", "j"), ("w", "v"), ("qu", "kv"), ("ck", "k"),
            ("ph", "f"), ("th", "t"), ("sh", "s"), ("tch", "c"), ("ch", "c"),
            ("dj", "d"), ("dz", "z"), ("ee", "i"), ("oo", "u"), ("c", "k")]

# Rule 5: the diacritic pairs this library keeps losing.
DIA = [("ć", "c"), ("č", "c"), ("đ", "dj"), ("đ", "d"), ("dž", "dz"),
       ("š", "s"), ("ž", "z")]


def fold_case(s):
    return unicodedata.normalize("NFC", (s or "").strip().lower())


def strip_dia(s):
    """-> the name with every diacritic and digraph flattened, so 'Đorđe',
    'Djordje' and 'Dorde' land on one key."""
    s = fold_case(s)
    for a, b in (("đ", "dj"), ("dž", "dz")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s)


def translit_key(s):
    """-> a key where English and Croatian spellings of one sound collide."""
    s = strip_dia(s)
    for a, b in TRANSLIT:
        s = s.replace(a, b)
    # After the substitutions "ks" and "kks" must still agree.
    return re.sub(r"(.)\1+", r"\1", s)


def letters_only(s):
    return [c for c in (s or "") if c.isalpha()]


def load_names(review_path, canon_path):
    """-> {written name: [files]}, the artist as write_tags would write it."""
    rows = json.load(open(review_path))
    mapping = {}
    try:
        canon = json.load(open(canon_path))
        mapping = canon.get("mapping") or {}
    except (OSError, ValueError):
        print("  no artist_canon.json: auditing raw proposals", file=sys.stderr)

    def canonical(a):
        # artist_canon keys are folded the way artist_names.fold folds them.
        return mapping.get(strip_dia(a), a)

    fields, comps = defaultdict(list), defaultdict(list)
    for r in rows:
        a = r.get("proposed_artist")
        if not a:
            continue
        name = canonical(a)
        fields[name].append(r["file"])
        for part in components(name):
            comps[canonical(part)].append(r["file"])
    return fields, comps


def load_tags(root):
    """-> ({field: [files]}, {artist: [files]}) read from the written audio.

    review.json is the proposal; this is what the phone actually shows. They
    drift, because output/_all is only rewritten when write_tags runs.

    easy=True deliberately: it maps TPE1, (c)ART and FLAC's "artist" to one key,
    and returns str for all three. Reading the MP4 atom directly would hand
    back raw bytes for a freeform atom and print as b'...'.
    """
    import mutagen

    fields, comps = defaultdict(list), defaultdict(list)
    exts = (".mp3", ".m4a", ".flac", ".opus", ".ogg", ".aac", ".mp4")
    import os
    for dirpath, _, filenames in os.walk(root):
        for fn in sorted(filenames):
            if not fn.lower().endswith(exts):
                continue
            path = os.path.join(dirpath, fn)
            try:
                audio = mutagen.File(path, easy=True)
            except Exception as exc:                    # noqa: BLE001
                print(f"  unreadable: {fn}: {exc}", file=sys.stderr)
                continue
            if audio is None or not audio.tags:
                print(f"  no tags: {fn}", file=sys.stderr)
                continue
            for value in audio.tags.get("artist") or []:
                if not value:
                    continue
                fields[value].append(fn)
                for part in components(value):
                    comps[part].append(fn)
    return fields, comps


def audit(names):
    """-> {rule: [(name, count, detail)]}, every rule the user listed."""
    hits = defaultdict(list)

    for n, files in sorted(names.items()):
        c = len(files)

        if any(q in n for q in QUOTES):
            hits["quotes"].append((n, c, "".join(q for q in QUOTES if q in n)))

        if any(b in n for b in BRACKETS):
            hits["brackets"].append((n, c, "".join(b for b in BRACKETS if b in n)))

        runs = CAPS_RUN.findall(n)
        # A name that is capitals throughout is a style (TTM, XXXTENTACION);
        # a run inside an otherwise normal name is damage. Report both, split.
        if runs:
            allcaps = "".join(letters_only(n)).isupper()
            hits["caps_allcaps" if allcaps else "caps_run"].append(
                (n, c, ",".join(runs)))

        if FEAT.search(n):
            hits["feat"].append((n, c, FEAT.search(n).group(0)))

        if REMIX.search(n):
            hits["remix"].append((n, c, REMIX.search(n).group(0)))

        nl = len(letters_only(n))
        if nl <= 2:
            hits["short"].append((n, c, f"{nl} letter(s)"))

        joiners = JOINER_RE.findall(n)
        if joiners:
            hits["joiner"].append((n, c, " ".join(sorted(set(joiners)))))

        odd = sorted({ch for ch in n
                      if not ch.isalnum() and ch not in ALLOWED_PUNCT
                      and ch not in BRACKETS and ch not in QUOTES})
        if odd:
            hits["symbols"].append((n, c, "".join(odd)))

    # Pairwise rules: two names in the library that are one artist.
    for rule, keyfn in (("diacritics", strip_dia), ("translit", translit_key)):
        groups = defaultdict(list)
        for n in names:
            groups[keyfn(n)].append(n)
        for key, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            if rule == "translit" and len({strip_dia(m) for m in members}) < 2:
                continue        # already reported as a diacritic pair
            total = sum(len(names[m]) for m in members)
            detail = " | ".join(f"{m} ({len(names[m])})" for m in sorted(members))
            hits[rule].append((key, total, detail))
    return hits


TITLES = {
    "quotes": "Rule 1: quotes or double quotes",
    "brackets": "Rule 2: brackets of any kind",
    "caps_run": "Rule 3a: 3+ capitals inside an otherwise normal name",
    "caps_allcaps": "Rule 3b: name is all capitals (may be the real style)",
    "translit": "Rule 4: English/Croatian transliteration pairs",
    "diacritics": "Rule 5: diacritic variants of one name",
    "symbols": "Rule 6: custom symbols in the name",
    "feat": "Rule 7: feat./ft./featuring in the artist field",
    "short": "Rule 8: one- or two-letter names",
    "joiner": "Rule 9: joining words (and, &, i, x, zajedno, ...)",
    "remix": "Rule 10: the word remix",
}


def selftest():
    """Prove the pair rules fire on names known to break them.

    Rule 4 and rule 5 report nothing when they work and nothing when they are
    broken, so an empty section is only evidence once these pass.
    """
    cases = [
        ("translit", {"Aleksandra Prijovic": 1, "Alexandra Prijovic": 1}, True),
        ("translit", {"Maks": 1, "Max": 1}, True),
        ("diacritics", {"Đorđe Balašević": 1, "Djordje Balasevic": 1}, True),
        ("diacritics", {"Ceca": 1, "Ćeća": 1}, True),
        # Must NOT fire: two different people, similar spelling.
        ("diacritics", {"Milan Stanković": 1, "Milan Stanojević": 1}, False),
        ("translit", {"Coby": 1, "Toby": 1}, False),
    ]
    bad = 0
    for rule, names, want in cases:
        got = bool(audit({k: ["f"] * v for k, v in names.items()}).get(rule))
        mark = "ok " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"    {mark} {rule:11} {' / '.join(names)}"
              f"   expected {'a hit' if want else 'no hit'}")
    print(f"\n  selftest: {len(cases) - bad}/{len(cases)} passed\n")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--review")
    ap.add_argument("--canon")
    ap.add_argument("--tags", help="read the written tags in this directory")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()

    if args.selftest:
        return 1 if selftest() else 0
    if args.tags:
        fields, names = load_tags(args.tags)
    elif args.review and args.canon:
        fields, names = load_names(args.review, args.canon)
    else:
        ap.error("need --tags, or --review with --canon, or --selftest")
    hits = audit(names)

    print(f"\n  {len(fields)} distinct artist fields, {len(names)} distinct "
          f"artists after splitting on '{SEPARATOR}', over "
          f"{sum(len(v) for v in fields.values())} tracks\n")
    for rule in TITLES:
        rows = hits.get(rule) or []
        print(f"\n{TITLES[rule]}  --  {len(rows)} hit(s)")
        for name, count, detail in sorted(rows, key=lambda r: -r[1]):
            print(f"    {name[:44]:44} {count:4} track(s)   {detail}")

    if args.json:
        json.dump({r: hits.get(r, []) for r in TITLES},
                  open(args.json, "w"), ensure_ascii=False, indent=1)
        print(f"\n  -> {args.json}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
