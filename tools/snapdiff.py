#!/usr/bin/env python3
"""Diff two snapshots against what the change was supposed to do.

A plain before/after diff answers "what changed", which is not the question.
The two questions are "did it fix what I meant to fix" and "did it change
anything else", and neither can be answered without knowing the intent. So
the intent is an input: write the tracks you expect to change to targets.txt
before making the change, and this splits the diff five ways.

  fixed       declared, and it changed. The fix works.
  missed      declared, and nothing moved. The fix did nothing.
  collateral  changed, never declared. The fix broke something else.
  vanished    stopped being written at all. The silent one.
  appeared    started being written. Sometimes the point, sometimes a double.

Missed, collateral, an unresolved target, and a vanish or an appearance that
was never declared each exit 1. A declared vanish or appearance does not: a
dedupe fix drops a copy on purpose.

targets.txt takes one entry per line: a source path, a fingerprint or an
output basename, optionally followed by a tab and the single field expected
to move. Blank lines and # comments are ignored. An empty file is a real
assertion, not a missing one: it says this change should alter nothing.

`--by-cohort` splits every count by artist locale and by container, because a
total can improve while half the library gets worse.

Usage: snapdiff.py --issue m4a-genres [--by-cohort]
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Enough to see the shape of a problem without burying it. The true count is
# always printed: a silently truncated list reads as "all clear".
SHOW = 40

# The two axes on which a change can look like a win overall while making half
# the library worse. m4a is a quarter of these files and the MP4 tag path has
# silently written fewer tags than the MP3 one before; the Balkan tracks are
# the ones every external catalogue is thinnest on, so a source that helps the
# English-language half and hurts the rest reads as an improvement until the
# split is visible.
COHORT_AXES = ("locale", "container")

# The outcomes worth splitting. Ordered worst-last so the table reads down to
# the number that decides whether a change ships.
OUTCOMES = ("tracks", "fixed", "missed", "collateral", "vanished", "appeared")

# A track that was already auto-accepted and whose name then changed is the
# expensive failure: the pipeline was confident and wrong. Counted apart from
# collateral, which treats every field alike, because this is the one that
# ships a wrong artist to the phone.
IDENTITY_FIELDS = ("review.proposed_artist", "review.proposed_title")


def load_json(path, what):
    if not os.path.exists(path):
        sys.exit(f"no {what} at {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_targets(path, tracks):
    """-> ({source path: {field, ...} or None}, unresolved).

    None means "expected to change, no particular field". Resolving by
    fingerprint and by basename as well as by path is not a convenience: the
    fingerprint is what the caches key analysis on and the basename is what a
    person reads off the review spreadsheet, and a target nobody can resolve
    would quietly become zero targets.
    """
    by_fp = {t["fp"]: t["path"] for t in tracks if t.get("fp")}
    known = {t["path"] for t in tracks}
    by_file = {}
    for t in tracks:
        by_file.setdefault(os.path.basename(t["path"]), []).append(t["path"])

    targets, unresolved = {}, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            # Only a leading # is a comment. Stripping from anywhere would
            # truncate "Artist - Song #1.mp3" into a path that does not
            # exist, and the target would vanish rather than fail loudly.
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            entry, _, field = line.rstrip("\n").partition("\t")
            entry, field = entry.strip(), field.strip()
            # An absolute path is checked against the sample like everything
            # else. Trusting it because it looks like a path is how a typo
            # becomes a target that matches nothing, is never reported as
            # unresolved, and can therefore never be missed.
            if entry in by_fp:
                paths = [by_fp[entry]]
            elif entry in known:
                paths = [entry]
            else:
                paths = by_file.get(entry, [])
            if not paths:
                unresolved.append(entry)
                continue
            for p in paths:
                if field:
                    targets.setdefault(p, set())
                    if targets[p] is not None:
                        targets[p].add(field)
                else:
                    targets[p] = None
    return targets, unresolved


def load_balkan():
    """-> fingerprint.likely_balkan, or exit saying why.

    Imported rather than reimplemented. A lookalike copy of the heuristic would
    grade a change against a different definition of the cohort than the
    pipeline itself uses, which is exactly the drift tools/audit_cutoff.py
    imports analyze.py to avoid. Exits rather than disabling the split
    silently: a cohort report that quietly became one column reads as "no
    difference between cohorts".
    """
    sys.path.insert(0, HERE)
    try:
        from pipeline.fingerprint import likely_balkan
    except Exception as e:                                   # pragma: no cover
        sys.exit(f"--by-cohort needs pipeline.fingerprint.likely_balkan "
                 f"and could not import it: {e}")
    return likely_balkan


def cohorts(track, is_balkan):
    """-> {axis: label} for one sampled track.

    Read off sample.json, which is frozen when the sample is drawn, rather than
    off either snapshot. A cohort computed from the snapshots could move
    between before and after -- a fix that corrects an artist could move a
    track from `other` to `balkan` -- and a track that changes cohort mid-diff
    makes both halves of the comparison meaningless.

    The locale test runs over artist, title and filename together because the
    proposed name is blank for exactly the tracks this cohort is about: the
    ones nothing identified. The filename is all the evidence left.
    """
    text = " ".join(x for x in (track.get("artist"), track.get("title"),
                                track.get("file")
                                or os.path.basename(track["path"])) if x)
    return {"locale": "balkan" if is_balkan(text) else "other",
            "container": os.path.splitext(track["path"])[1].lower() or "(none)"}


def show_cohorts(tally, labels):
    """One table per axis: outcomes down, cohort labels across."""
    for axis in COHORT_AXES:
        cols = sorted(labels[axis])
        if len(cols) < 2:
            # One column is not a split. Say so rather than printing a table
            # that looks like a comparison and is not.
            only = cols[0] if cols else "nothing"
            print(f"\n  by {axis}: every sampled track is {only}, "
                  f"so there is nothing to compare")
            continue
        print(f"\n  by {axis}:")
        print("    " + "outcome".ljust(12) + "".join(c.rjust(12) for c in cols))
        for outcome in OUTCOMES:
            row = "".join(str(tally[axis][(outcome, c)]).rjust(12)
                          for c in cols)
            print("    " + outcome.ljust(12) + row)


def changes(before, after):
    """-> [(field, before, after), ...] for one track."""
    out = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b != a:
            out.append((k, b, a))
    return out


def show(title, rows, note=""):
    print(f"\n{title}  {len(rows)}{note}")
    if not rows:
        return
    for r in rows[:SHOW]:
        print(f"    {r}")
    if len(rows) > SHOW:
        print(f"    ... and {len(rows) - SHOW} more (of {len(rows)})")


def main():
    ap = argparse.ArgumentParser(
        description="Split a before/after diff into fixed, missed and "
                    "collateral.")
    ap.add_argument("--issue", required=True)
    ap.add_argument("--baseline", default=os.path.join(HERE, "baseline"))
    ap.add_argument("--before", default="before")
    ap.add_argument("--after", default="after")
    ap.add_argument("--targets", default=None,
                    help="default baseline/<issue>/targets.txt")
    ap.add_argument("--by-cohort", action="store_true",
                    help="split every count by artist locale and container")
    args = ap.parse_args()

    root = os.path.join(args.baseline, args.issue)
    before = load_json(os.path.join(root, f"{args.before}.json"), "snapshot")
    after = load_json(os.path.join(root, f"{args.after}.json"), "snapshot")
    sample = load_json(os.path.join(root, "sample.json"), "sample")

    if before["level"] != after["level"]:
        sys.exit(f"the two snapshots were taken at different levels "
                 f"({before['level']} then {after['level']}); everything the "
                 f"second one did not look at would read as a change")

    tpath = args.targets or os.path.join(root, "targets.txt")
    if not os.path.exists(tpath):
        sys.exit(
            f"no {tpath}. Declare what you expect to change before you change "
            f"it: one source path, fingerprint or filename per line. Written "
            f"afterwards it is a description of what happened, not a test. An "
            f"empty file is allowed and means nothing should change.")
    targets, unresolved = read_targets(tpath, sample["tracks"])

    b_tracks, a_tracks = before["tracks"], after["tracks"]

    # "Still there" means something different at each level. At out level it
    # is whether a file was written; at cache level nothing has been written
    # yet, so it is whether the track still reaches write_tags at all.
    out_level = before["level"] in ("out", "both")

    def present(rec):
        if out_level:
            return "out.relpath" in rec
        return any(k.startswith(("analysis.", "review.")) for k in rec)

    fixed, missed, collateral = [], [], []
    vanished, appeared, undeclared_gone = [], [], []
    collateral_fields, changed_tracks = Counter(), set()
    # An auto-accepted row whose name then moved. Split by whether it was
    # declared: declared is a fix landing on a track that was confidently
    # wrong, undeclared is a change nobody asked for on a track nobody was
    # going to check.
    false_auto, false_auto_undeclared = [], []
    # The tier and the proposed name live in the cache snapshot. An `out`
    # snapshot records what landed in the file and carries neither, so the
    # count there would be a permanent zero that reads as "none happened".
    # Say it cannot be measured instead.
    can_false_auto = before["level"] in ("cache", "both")

    is_balkan = load_balkan() if args.by_cohort else None
    sampled = {t["path"]: t for t in sample["tracks"]}
    tally, labels = defaultdict(Counter), defaultdict(set)

    def bump(outcome, path, n=1):
        """Count one outcome against every cohort the track belongs to.

        A track missing from sample.json cannot be placed, and guessing a
        cohort for it would put real numbers in the wrong column. It is
        counted under `(unsampled)` so the totals still add up.
        """
        if not args.by_cohort:
            return
        t = sampled.get(path)
        got = (cohorts(t, is_balkan) if t
               else {axis: "(unsampled)" for axis in COHORT_AXES})
        for axis, label in got.items():
            labels[axis].add(label)
            tally[axis][(outcome, label)] += n

    for p in sorted(set(b_tracks) | set(a_tracks)):
        b, a = b_tracks.get(p, {}), a_tracks.get(p, {})
        name = os.path.basename(p)
        bump("tracks", p)

        # A track that stopped being written is not a field change, it is a
        # track that left the library. A field-level diff shows that as a
        # handful of blanked tags, which reads like a tagging tweak.
        #
        # Declaring the track makes the disappearance intended, because
        # sometimes it is the whole point: a dedupe fix drops a copy on
        # purpose. Undeclared ones still fail. A tool that calls every
        # intended removal a regression is a tool people learn to ignore.
        if present(b) and not present(a):
            mark = "  (declared)" if p in targets else ""
            vanished.append(f"{name}  was "
                            f"{b.get('out.relpath', 'present')}{mark}")
            bump("vanished", p)
            if p not in targets:
                undeclared_gone.append(p)
            continue
        if present(a) and not present(b):
            mark = "  (declared)" if p in targets else ""
            appeared.append(f"{name}  now "
                            f"{a.get('out.relpath', 'present')}{mark}")
            bump("appeared", p)
            if p not in targets:
                undeclared_gone.append(p)
            continue

        diff = changes(b, a)
        if diff:
            changed_tracks.add(p)
        declared = p in targets
        want = targets.get(p)

        # An auto-accepted row is one the pipeline said it did not need a human
        # for. If its artist or title then moves, the old value shipped without
        # anyone ever being asked to look at it, which is the failure this
        # library is tagged to avoid. Read the tier from BEFORE: the question
        # is what the previous build was confident about, not what this one is.
        if (can_false_auto and b.get("review.tier") == "auto"
                and any(f in IDENTITY_FIELDS for f, _, _ in diff)):
            moved = ", ".join(f"{f.split('.')[-1]} {bv!r} -> {av!r}"
                              for f, bv, av in diff if f in IDENTITY_FIELDS)
            line = f"{name}  was auto at {b.get('review.confidence')}  {moved}"
            false_auto.append(line)
            if not declared:
                false_auto_undeclared.append(line)

        for field, bv, av in diff:
            line = f"{name}  {field}  {bv!r} -> {av!r}"
            if declared and (want is None or field in want):
                fixed.append(line)
                bump("fixed", p)
            else:
                collateral_fields[field] += 1
                collateral.append(line)
                bump("collateral", p)

        if declared:
            if not diff:
                missed.append(f"{name}  nothing changed")
                bump("missed", p)
            elif want:
                moved = {f for f, _, _ in diff}
                for field in sorted(want - moved):
                    missed.append(f"{name}  {field} did not change")
                    bump("missed", p)

    if unresolved:
        show("UNRESOLVED  targets that match no sampled track", unresolved,
             "  (a typo here is a target that silently does not exist)")
    show("FIXED       declared and changed", fixed)
    show("MISSED      declared and unchanged, the fix did nothing", missed)
    show("COLLATERAL  changed and never declared", collateral)
    show("VANISHED    no longer written at all", vanished)
    show("APPEARED    written now, was not before", appeared,
         "  (a declared one is intended; an undeclared one is not)")

    if can_false_auto:
        show("FALSE AUTO  was auto-accepted, and the name moved anyway",
             false_auto,
             "  (declared means the fix reached a track that shipped wrong)")
    else:
        print(f"\nFALSE AUTO  not measurable at level '{before['level']}': "
              f"the tier and the proposed name are cache fields")

    if collateral:
        print("\n  collateral by field:")
        for field, n in collateral_fields.most_common():
            print(f"    {field:44s} {n}")

    if args.by_cohort:
        show_cohorts(tally, labels)

    n = len(set(b_tracks) | set(a_tracks))
    print(f"\n  {n} tracks sampled, {len(changed_tracks)} changed, "
          f"{len(targets)} declared")
    print(f"  fixed {len(fixed)}, missed {len(missed)}, "
          f"collateral {len(collateral)}, vanished {len(vanished)}, "
          f"appeared {len(appeared)}"
          + (f" ({len(undeclared_gone)} of those undeclared)"
             if undeclared_gone else ""))
    # Not added to `bad`: an undeclared one is already counted as collateral,
    # and a declared one is the fix doing its job. It is printed on its own
    # line because it is the number worth reading first, not because it is a
    # separate failure.
    if can_false_auto:
        print(f"  false auto-accepts {len(false_auto)}"
              + (f" ({len(false_auto_undeclared)} undeclared)"
                 if false_auto_undeclared else ""))

    bad = (len(missed) + len(collateral) + len(undeclared_gone)
           + len(unresolved))
    if bad:
        print("\n  NOT CLEAN. Every line above is either a fix that did not "
              "land or a change nobody asked for.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
