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

Anything in the last three, or in missed, exits 1.

targets.txt takes one entry per line: a source path, a fingerprint or an
output basename, optionally followed by a tab and the single field expected
to move. Blank lines and # comments are ignored. An empty file is a real
assertion, not a missing one: it says this change should alter nothing.

Usage: snapdiff.py --issue m4a-genres
"""
import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Enough to see the shape of a problem without burying it. The true count is
# always printed: a silently truncated list reads as "all clear".
SHOW = 40


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
            line = line.split("#")[0].rstrip()
            if not line.strip():
                continue
            entry, _, field = line.partition("\t")
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
    vanished, appeared = [], []
    collateral_fields, changed_tracks = Counter(), set()

    for p in sorted(set(b_tracks) | set(a_tracks)):
        b, a = b_tracks.get(p, {}), a_tracks.get(p, {})
        name = os.path.basename(p)

        # A track that stopped being written is not a field change, it is a
        # track that left the library. A field-level diff shows that as a
        # handful of blanked tags, which reads like a tagging tweak.
        if present(b) and not present(a):
            vanished.append(f"{name}  was {b.get('out.relpath', 'present')}")
            continue
        if present(a) and not present(b):
            appeared.append(f"{name}  now {a.get('out.relpath', 'present')}")
            continue

        diff = changes(b, a)
        if diff:
            changed_tracks.add(p)
        declared = p in targets
        want = targets.get(p)

        for field, bv, av in diff:
            line = f"{name}  {field}  {bv!r} -> {av!r}"
            if declared and (want is None or field in want):
                fixed.append(line)
            else:
                collateral_fields[field] += 1
                collateral.append(line)

        if declared:
            if not diff:
                missed.append(f"{name}  nothing changed")
            elif want:
                moved = {f for f, _, _ in diff}
                for field in sorted(want - moved):
                    missed.append(f"{name}  {field} did not change")

    if unresolved:
        show("UNRESOLVED  targets that match no sampled track", unresolved,
             "  (a typo here is a target that silently does not exist)")
    show("FIXED       declared and changed", fixed)
    show("MISSED      declared and unchanged, the fix did nothing", missed)
    show("COLLATERAL  changed and never declared", collateral)
    show("VANISHED    no longer written at all", vanished)
    show("APPEARED    written now, was not before", appeared)

    if collateral:
        print("\n  collateral by field:")
        for field, n in collateral_fields.most_common():
            print(f"    {field:44s} {n}")

    n = len(set(b_tracks) | set(a_tracks))
    print(f"\n  {n} tracks sampled, {len(changed_tracks)} changed, "
          f"{len(targets)} declared")
    print(f"  fixed {len(fixed)}, missed {len(missed)}, "
          f"collateral {len(collateral)}, vanished {len(vanished)}, "
          f"appeared {len(appeared)}")

    bad = len(missed) + len(collateral) + len(vanished) + len(unresolved)
    if bad:
        print("\n  NOT CLEAN. Every line above is either a fix that did not "
              "land or a change nobody asked for.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
