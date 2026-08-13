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

Matching is at a path boundary, so "Music Mine" never matches "Music Mine 2",
and each string is rewritten at most once, longest prefix first, so a nested
pair cannot be applied twice.

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


def walk(node, pairs, count):
    """Rewrite every string in a JSON tree, keys included."""
    if isinstance(node, str):
        s, hit = rewrite(node, pairs)
        if hit:
            count[0] += 1
        return s
    if isinstance(node, list):
        return [walk(x, pairs, count) for x in node]
    if isinstance(node, dict):
        return {walk(k, pairs, count): walk(v, pairs, count)
                for k, v in node.items()}
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
        pairs.append((old.rstrip(os.sep), new.rstrip(os.sep)))
    # Longest first, so a pair nested inside another cannot be applied to a
    # string the outer pair has already rewritten.
    pairs.sort(key=lambda p: len(p[0]), reverse=True)

    for old, new in pairs:
        if args.apply and not os.path.isdir(new):
            sys.exit(f"the new folder does not exist, so this would point "
                     f"every cache at nothing: {new}")

    total, touched = 0, []
    for path in sorted(glob.glob(os.path.join(CACHE, "*.json"))):
        name = os.path.basename(path)
        if ".bak" in name:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            print(f"  {name:26} unreadable, left alone")
            continue
        count = [0]
        new_data = walk(data, pairs, count)
        if not count[0]:
            continue
        total += count[0]
        touched.append((name, count[0]))
        if args.apply:
            # Backed up once, then replaced atomically: a half-written cache
            # is worse than a stale one, because nothing downstream can tell.
            bak = path + ".bak-relocate"
            if not os.path.exists(bak):
                shutil.copy2(path, bak)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(new_data, fh, ensure_ascii=False)
            os.replace(tmp, path)

    for name, n in touched:
        print(f"  {name:26} {n:6} strings")
    verb = "rewritten" if args.apply else "would be rewritten"
    print(f"\n  {total} strings across {len(touched)} caches {verb}")
    if not args.apply:
        print("  nothing was written. Pass --apply to do it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
