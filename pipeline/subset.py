#!/usr/bin/env python3
"""Read a `--only` list: the tracks one run is restricted to.

Verifying a change means running one stage over a frozen sample rather than
over the library, so three stages take a `--only` file and a fourth will. They
were about to hold three copies of the same twenty lines, and the copy that
drifts is the one that silently selects a different subset than the sample it
was given.

The file takes one entry per line: a source path or a fingerprint. Blank lines
and lines starting with `#` are ignored.

Only a LEADING `#` is a comment. Stripping from anywhere would turn a real
filename like `Artist - Song #1.mp3` into a path that does not exist, and the
track would drop out of the subset without anything saying so.

Entries that resolve to nothing are returned rather than discarded. A list
whose paths match nothing produces an empty selection, which behaves exactly
like a clean run over everything and is the failure this file is shaped to
prevent. What to do about it differs per stage, so that decision stays with
the caller.
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")


def fingerprint_paths(analysis_path=ANALYSIS):
    """-> {fingerprint: source path}, so a sample can be named either way.

    The fingerprint is what `tools/sample.py` keys its draw on, because it
    survives a rename; the path is what the stage caches key on. A subset file
    may hold either.
    """
    if not os.path.exists(analysis_path):
        return {}
    try:
        with open(analysis_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {fp: v["path"] for fp, v in data.items() if v.get("path")}


def read(list_path, known=None, analysis_path=ANALYSIS):
    """-> (wanted paths, unresolved entries).

    `known` is the set of paths the caller can actually act on. Given one, an
    entry outside it is reported as unresolved instead of being selected and
    then quietly doing nothing.
    """
    by_fp = fingerprint_paths(analysis_path)
    wanted, unresolved = set(), []
    with open(list_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            entry = line.strip()
            path = by_fp.get(entry, entry)
            if known is not None and path not in known:
                unresolved.append(entry)
            else:
                wanted.add(path)
    return wanted, unresolved


def report(list_path, unresolved, limit=10):
    """Print the entries that matched nothing. Says the count before the list,
    because a truncated list with no total reads as the whole problem."""
    if not unresolved:
        return
    print(f"  {len(unresolved)} entries in {list_path} match no known track:")
    for u in unresolved[:limit]:
        print(f"    {u}")
    if len(unresolved) > limit:
        print(f"    ... and {len(unresolved) - limit} more")
