#!/usr/bin/env python3
"""How much agreement a field actually has, counted in independent families.

`review.score` grades a match on how well one proposal fits the filename. That
answers "is this plausible" and cannot answer "did anything else agree", which
is a different question and often the deciding one. The evidence store holds
every answer every source gave, including the ones that lost, so the second
question is now answerable.

Counted in families, never in sources. Cover Art Archive agreeing with
MusicBrainz is MusicBrainz agreeing with itself, and the file's own tag
agreeing with its filename is one bad download name agreeing with itself.
`evidence.FAMILY` is where that grouping lives.

What this deliberately does NOT do:

Duration is not a constraint here, hard or soft. Gating on it threw away a
third of the correct matches and, reused as a link filter, a fifth of the
answers given by hand, because these files are YouTube rips whose intros the
streaming single does not have.

Nothing here raises a score. Agreement between independent families is real
evidence and will eventually be worth something, but confidence that goes up
moves tracks out of review unseen, and the bar those tracks would cross was
calibrated against a scorer that did not have this. Dissent lowers; agreement
is recorded and left for a change that can be measured on its own.

Usage:
  confidence.py                 # what the store says about the library
  confidence.py --field artist
"""
import argparse
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import evidence  # noqa: E402

# The fields where a wrong value is worse than a missing one, and where a
# disagreement is therefore worth interrupting for. Deliberately not every
# field: sources disagree about `year` constantly, because a reissue date and
# an original release date are both true, and treating that as a contradiction
# would send most of the library to review to answer a question nobody asked.
IDENTITY = ("artist", "title")

# How much a contested identity is discounted. One multiplier, applied once,
# rather than a scale: the honest claim is "something else says otherwise",
# not a measurement of how wrong it is. 0.8 is enough to drop a lone
# catalogue's 0.80 below the review bar without touching a fingerprint match
# or anything a human confirmed.
DISSENT = 0.8

# A dissent from a source that only ever repeats what is already on the file
# is not a second opinion. `local` is the file's own tags, its name and its
# folder, which all descend from the same download.
IGNORE_DISSENT = {"local"}


def _rows(conn, path, field):
    return [r for r in evidence.observations(conn, path, field)
            if r["state"] == evidence.FOUND and r["value_norm"]]


def agreement(conn, path, field):
    """-> {'value', 'agree', 'dissent', 'families', 'human', 'audio'} or None.

    `agree` counts the families behind the most-supported value, `dissent` the
    families behind every other value. A family that says both things counts
    once for agreement and not at all as dissent: a catalogue holding two
    pressings is not arguing with itself.
    """
    rows = _rows(conn, path, field)
    if not rows:
        return None
    by_value = defaultdict(set)
    for r in rows:
        by_value[r["value_norm"]].add(r["family"])
    shown = {r["value_norm"]: r["value"] for r in rows}

    # A value you answered wins outright, however many catalogues prefer
    # another. Ranking on family count alone let two catalogues outvote a
    # hand-given answer, which flagged 28 artists as contested where the only
    # thing contesting them was a database disagreeing with the person who
    # already settled it.
    #
    # Then by families, then by the value's own text, so a tie breaks the same
    # way on every run rather than by dict order.
    def rank(v):
        return (evidence.HUMAN in by_value[v], len(by_value[v]), v)

    top = max(by_value, key=rank)
    agree = by_value[top]
    dissent = set()
    for value, fams in by_value.items():
        if value != top:
            dissent |= fams - agree
    dissent -= IGNORE_DISSENT
    return {"value": shown[top], "agree": sorted(agree),
            "dissent": sorted(dissent),
            "families": sorted(set().union(*by_value.values())),
            "human": evidence.HUMAN in agree,
            "audio": any(r["source"] in evidence.AUDIO_DERIVED
                         for r in rows if r["value_norm"] == top)}


def contested(conn, path, proposed):
    """-> [(field, winning value, dissenting families), ...] worth flagging.

    Only where the proposal itself is the thing being argued with. A field the
    store disagrees about while the pipeline proposed something else again is
    a different and larger problem, and one this is not the place to raise.
    """
    out = []
    for field in IDENTITY:
        want = proposed.get(field)
        if not want:
            continue
        got = agreement(conn, path, field)
        if not got or not got["dissent"]:
            continue
        # A human answer settles it. It outranks every lookup by construction,
        # so a catalogue disagreeing with it is not a reason to ask again.
        if got["human"]:
            continue
        if evidence.norm(want) != evidence.norm(got["value"]):
            continue
        out.append((field, got["value"], got["dissent"]))
    return out


def penalty(conn, path, proposed):
    """-> (multiplier, [reason, ...]) for one track's proposed identity."""
    hits = contested(conn, path, proposed)
    if not hits:
        return 1.0, []
    reasons = [f"{f} contested by {', '.join(fams)}" for f, _v, fams in hits]
    # Applied once however many fields are contested. Two disagreements are
    # one problem with the identity, not two independent halvings.
    return DISSENT, reasons


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--field", action="append",
                    help="only these fields (repeatable)")
    ap.add_argument("--db", default=evidence.DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no store at {args.db}; run "
                 f"pipeline/evidence.py --backfill first")
    conn = evidence.connect(args.db, readonly=True)
    fields = args.field or list(IDENTITY)

    print()
    for field in fields:
        rows = conn.execute(
            "SELECT DISTINCT track_path FROM observation WHERE field=?",
            (field,)).fetchall()
        contested_n, families = 0, defaultdict(int)
        for (path,) in rows:
            got = agreement(conn, path, field)
            if got and got["dissent"]:
                contested_n += 1
                for f in got["dissent"]:
                    families[f] += 1
        print(f"  {field}: {contested_n} of {len(rows)} tracks contested")
        for f, n in sorted(families.items(), key=lambda kv: -kv[1]):
            print(f"    dissenting family {f:14s} {n}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
