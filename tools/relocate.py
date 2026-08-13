#!/usr/bin/env python3
"""Rewrite the paths in every cache after the music has been moved.

Moving a source folder is cheap. Losing the caches that describe it is not:
fourteen of them are keyed by absolute path, and two of those, `lyric_align`
and `lyric_verify`, are hours of Whisper each. Re-running the pipeline after a
move without this would silently rebuild all of it, and "silently" is the
problem: nothing errors, the work simply happens again.

So: move the files, then rewrite the keys. Every string anywhere in every
cache that begins with an old folder is rewritten to begin with the new one,
whether it is a dict key, a value, or buried in a list.

`hints.tsv` needs nothing. It is keyed by filename, so every answer you have
ever given survives a move on its own.

One caveat this cannot fix: `fingerprints.json` keys on `path|size|mtime`, so
a move that does not preserve the modification time leaves those entries dead
even with the path corrected. `rsync -a` and `mv` both preserve it; a copy
through a file manager may not. Check that `fingerprint` reports everything
cached afterwards, not just that this tool reported a number.

Matching is at a path boundary, so "Music Mine" never matches "Music Mine 2",
and each string is rewritten at most once, longest prefix first.

One pass applying at most one pair is provably the whole job, which is what
makes a second run a no-op: afterwards no string starts with any OLD. Keeping
that true is why the pairs are validated rather than merely sorted. A
destination inside its own source, two pairs whose paths overlap, and the
same source given twice are each refused, because none of them can be
expressed in one pass and all three produce paths belonging to no layout at
all while reporting healthy counts.

Usage:
  tools/relocate.py "/old/path=/new/path" ["/other/old=/other/new" ...]
  tools/relocate.py ... --apply          # without this it only reports
"""
import argparse
import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CACHE = os.path.join(HERE, "cache")


class KeyCollision(Exception):
    """Two cache keys would become one. Refused rather than merged."""

    def __init__(self, a, b, new):
        super().__init__(f"{a!r} and {b!r} would both become {new!r}")


def rewrite(s, pairs):
    """-> (new string, True) if a prefix matched, else (s, False).

    A prefix matches only at a path boundary: exactly equal, or followed by a
    separator. Without that, moving `.../Music Mine` would also rewrite
    `.../Music Mine 2`, which is a different folder.
    """
    for old, new in pairs:
        if s == old:
            return new, True
        if s.startswith(old + os.sep):
            return new + s[len(old):], True
    return s, False


def walk(node, pairs, count, per):
    """Rewrite every string in a JSON tree, keys included.

    Dict keys are rewritten into a fresh dict, and a collision there is
    refused rather than resolved: two source folders merged into one
    destination would otherwise drop every colliding entry with last-wins,
    losing cache data while the report still said it had succeeded.
    """
    if isinstance(node, str):
        s, hit = rewrite(node, pairs)
        if hit:
            count[0] += 1
            for i, (o, _) in enumerate(pairs):
                if node == o or node.startswith(o + os.sep):
                    per[i] += 1
                    break
        return s
    if isinstance(node, list):
        return [walk(x, pairs, count, per) for x in node]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            nk = walk(k, pairs, count, per)
            if nk in out:
                # Both originals, not just the second one. In the common
                # ordering the pre-move entry comes first and the post-move
                # entry is appended later, so naming only the key being
                # processed printed the same path on both sides and
                # identified nothing.
                first = next(o for o in node if walk(o, pairs, [0],
                                                     [0] * len(pairs)) == nk)
                raise KeyCollision(first, k, nk)
            out[nk] = walk(v, pairs, count, per)
        return out
    return node


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pair", nargs="+", metavar="OLD=NEW",
                    help="a folder that moved, and where it moved to")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes; without it, only report")
    args = ap.parse_args()

    pairs = []
    for p in args.pair:
        if "=" not in p:
            sys.exit(f"expected OLD=NEW, got: {p}")
        old, _, new = p.partition("=")
        old, new = old.rstrip(os.sep), new.rstrip(os.sep)
        # An empty or root OLD matches every absolute string in every cache
        # and rewrites the lot into nonsense. Cheap to refuse, unrecoverable
        # to discover afterwards.
        if not old or not new:
            sys.exit(f"both sides are required, got: {p}")
        if not os.path.isabs(old) or not os.path.isabs(new):
            sys.exit(f"both sides must be absolute paths, got: {p}")
        if old == new:
            sys.exit(f"nothing to do, both sides are the same: {old}")
        pairs.append((old, new))
    # Longest first, so a pair nested inside another cannot be applied to a
    # string the outer pair has already rewritten.
    pairs.sort(key=lambda p: len(p[0]), reverse=True)

    # Everything below exists so that one pass over each string, applying at
    # most one pair, is provably the whole job. That is what makes a second
    # run a no-op: after a move, no string starts with any OLD any more.
    def under(a, b):
        return a == b or a.startswith(b + os.sep)

    seen = {}
    for old_p, new_p in pairs:
        # The destination inside the source is the one shape a single pass
        # cannot express: the rewritten string still starts with OLD, so a
        # second run moves it again, /a/moved/moved/x. Moving a folder UP a
        # level is fine and stays allowed, because the result no longer
        # matches OLD.
        if under(new_p, old_p) and new_p != old_p:
            sys.exit(f"the destination is inside the source:\n  {old_p}\n"
                     f"  -> {new_p}\nMove it somewhere outside, or do it in "
                     f"two steps.")
        if old_p in seen:
            sys.exit(f"{old_p} is given twice, moving to {seen[old_p]} and "
                     f"to {new_p}. Only one can happen.")
        seen[old_p] = new_p

    # A chain, where one pair's destination overlaps another pair's source in
    # either direction, cannot be expressed as one pass either: /m/a/b=/m
    # with /m/a=/z sends a file to /z/b/song.mp3, a path in neither layout,
    # and both pairs report a healthy count while doing it. Refused rather
    # than ordered, because "which did you mean" is not ours to guess.
    for old_a, new_a in pairs:
        for old_b, _ in pairs:
            if old_a == old_b:
                continue
            if under(new_a, old_b) or under(old_b, new_a):
                sys.exit(f"these two overlap: {old_a} moves into {new_a}, "
                         f"which sits across {old_b}.\nRun them one at a "
                         f"time, or give the final destination for each.")

    for old, new in pairs:
        if args.apply and not os.path.isdir(new):
            sys.exit(f"the new folder does not exist, so this would point "
                     f"every cache at nothing: {new}")

    # Two passes. Every cache is rewritten in memory and checked before any
    # of them is written, because the failure this guards against is
    # discovered halfway through: exiting on a collision in the eleventh
    # cache after replacing ten of them leaves cache/ half relocated, and the
    # message saying nothing was written would be a lie.
    # cache/ exists only in the main checkout, so running a worktree's copy
    # would otherwise print "0 strings across 0 caches" and exit 0, which
    # reads as "those paths are not in your caches" rather than "I looked in
    # the wrong place".
    if not os.path.isdir(CACHE):
        sys.exit(f"no cache folder at {CACHE}. Run the copy in the checkout "
                 f"that owns the caches.")
    files = [f for f in sorted(glob.glob(os.path.join(CACHE, "*.json")))
             if ".bak" not in os.path.basename(f)]
    if not files:
        sys.exit(f"no caches to rewrite in {CACHE}.")

    total, touched, totals = 0, [], [0] * len(pairs)
    pending = []
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            print(f"  {name:26} unreadable, left alone")
            continue
        count, per = [0], [0] * len(pairs)
        try:
            new_data = walk(data, pairs, count, per)
        except KeyCollision as e:
            sys.exit(f"{name}: {e}\nNothing was written.")
        for i, n in enumerate(per):
            totals[i] += n
        if not count[0]:
            continue
        total += count[0]
        touched.append((name, count[0]))
        pending.append((path, new_data))

    if args.apply:
        for path, new_data in pending:
            # The backup is refreshed every run, not written once: it exists
            # to undo the run being made now, and keeping the first one
            # forever leaves every later move unprotected.
            shutil.copy2(path, path + ".bak-relocate")
            # Written to a temporary file and renamed, because a half-written
            # cache is worse than a stale one: nothing downstream can tell.
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(new_data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)

    for name, n in touched:
        print(f"  {name:26} {n:6} strings")
    verb = "rewritten" if args.apply else "would be rewritten"
    print(f"\n  {total} strings across {len(touched)} caches {verb}")
    # Per pair, because one misspelled OLD among several otherwise passes as
    # a successful run while half the caches still point at the old folder.
    for (old_p, _), n in zip(pairs, totals):
        flag = "" if n else "   <- matched nothing, check this path"
        print(f"    {n:6}  {old_p}{flag}")
    if not args.apply:
        print("  nothing was written. Pass --apply to do it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
