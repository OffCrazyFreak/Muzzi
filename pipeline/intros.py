#!/usr/bin/env python3
"""Files that open with the same recording: label bumpers, producer tags.

Some downloads carry a spoken or musical intro before the song. IDJVideos is
the best known here, but it is not the only one: measured on this library there
are at least four distinct openings shared across three or more files, on
different labels.

Measures only. Nothing here writes audio or decides anything; write_tags does
the cutting, and only for files a person has confirmed.

What this can and cannot tell you
---------------------------------

It finds files whose fingerprints agree for a run of items from the very start,
which means they begin with the same audio. That is necessary and nowhere near
sufficient. Eight NCS tracks share up to 8.5s of opening and none of them has a
bumper at all: dance tracks built on the same intro beat look exactly like a
shared recording from here, and cutting them would have removed the first bars
of eight songs.

So nothing is ever cut on detection alone. Each file becomes a review row
carrying its own boundary, and only an answer turns into a cut. That is also
why the boundary is per file rather than per cluster: within one group here the
shared opening runs from 8.40s down to 3.03s, and a single figure would cut
five seconds into the shortest ones.

Two rules the measurement itself has to get right:

  no chaining      single-link clustering joins A to C through B even when A
                   and C share nothing. Two NCS files were pulled into a group
                   they share 0.00s with. Every member is therefore re-checked
                   against the cluster's representative and dropped if it does
                   not stand on its own.
  run, not ratio   an earlier attempt scored overall bit agreement across the
                   opening and found a 34-file "cluster" whose members shared
                   no single fingerprint item. Chromaprint bits are not
                   uniformly distributed, so two unrelated quiet openings agree
                   on 92% of bits. Only a run of matching items from index 0
                   means "starts with the same thing".

Usage:
  intros.py                 # report what shares an opening
  intros.py --min-files 4
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

FINGERPRINTS = os.path.join(HERE, "cache", "fingerprints.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
OUT = os.path.join(HERE, "cache", "intros.json")

# How far into each file to compare. About 11s at this library's item rate,
# which is twice the longest confirmed intro here (7.5s) so a boundary is never
# clipped by the window itself.
ITEMS = 48
# A fingerprint item is 32 bits. Two recordings of the same bumper differ by a
# few bits per item through encoder noise; 6 was chosen because it separates
# the known-shared openings from unrelated files with a wide margin, and 32
# would match everything.
ITEM_BITS = 6
# The shortest run that counts as a shared opening. Below about 2.5s a match is
# as likely to be a common drum fill as a recording.
MIN_RUN = 12
# Fewer files than this is a coincidence or a duplicate, not a label's bumper.
MIN_FILES = 3


def _decode(blob):
    import chromaprint
    try:
        raw, _version = chromaprint.decode_fingerprint(blob.encode())
    except Exception:
        return None
    return raw


def load(path=None, analysis=None):
    """-> [{path, balkan, secs_per_item, items}] for every usable fingerprint."""
    import numpy as np
    fps = json.load(open(path or FINGERPRINTS))
    rows = []
    for v in fps.values():
        if not v.get("fingerprint") or not v.get("path"):
            continue
        raw = _decode(v["fingerprint"])
        if not raw or len(raw) < ITEMS:
            continue
        rows.append({"path": v["path"], "balkan": bool(v.get("likely_balkan")),
                     "secs_per_item": (v.get("duration") or 1) / len(raw),
                     "items": np.array(raw[:ITEMS], dtype=np.uint32)})
    return rows


def run_lengths(rows):
    """-> an n x n matrix of how many opening items each pair shares."""
    import numpy as np
    n = len(rows)
    M = np.stack([r["items"] for r in rows])
    run = np.zeros((n, n), dtype=np.int16)
    alive = np.ones((n, n), dtype=bool)
    for k in range(ITEMS):
        col = M[:, k]
        x = col[:, None] ^ col[None, :]
        # Bit count without popcount: the arrays are uint32 and numpy has no
        # vectorised popcount before 2.0, and this runs 48 times, not per pair.
        counted = np.zeros_like(x, dtype=np.uint8)
        t = x.copy()
        while t.any():
            counted += (t & 1).astype(np.uint8)
            t >>= 1
        alive &= counted <= ITEM_BITS
        run += alive
    return run


def clusters(rows, run, min_files=MIN_FILES):
    """-> [{files: [{path, shared_items, shared_secs}], ...}] worth asking about.

    Grown by union-find and then pruned: every member has to share MIN_RUN with
    the representative directly, not through a chain of intermediates.
    """
    import numpy as np
    n = len(rows)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    ii, jj = np.nonzero(run >= MIN_RUN)
    for a, b in zip(ii, jj):
        if a < b:
            ra, rb = find(int(a)), find(int(b))
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

    grouped = defaultdict(list)
    for i in range(n):
        grouped[find(i)].append(i)

    per_item = float(np.median([r["secs_per_item"] for r in rows]))
    out = []
    for members in grouped.values():
        if len(members) < min_files:
            continue
        # The representative is the member that agrees most with the rest: the
        # cleanest copy of whatever they share.
        def agreement(i):
            return float(np.median([run[i][j] for j in members if j != i]))

        rep = max(members, key=agreement)
        kept = [i for i in members if i == rep or run[rep][i] >= MIN_RUN]
        dropped = len(members) - len(kept)
        if len(kept) < min_files:
            continue
        files = []
        for i in sorted(kept, key=lambda i: -int(run[rep][i])):
            shared = int(run[rep][i]) if i != rep else int(agreement(i))
            files.append({"path": rows[i]["path"],
                          "file": os.path.basename(rows[i]["path"]),
                          "balkan": rows[i]["balkan"],
                          "shared_items": shared,
                          "shared_secs": round(shared * per_item, 2)})
        out.append({"representative": rows[rep]["path"],
                    "files": files,
                    "chained_out": dropped,
                    "balkan": sum(1 for f in files if f["balkan"]),
                    "median_secs": round(
                        float(np.median([f["shared_secs"] for f in files])), 2)})
    return sorted(out, key=lambda c: -len(c["files"]))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-files", type=int, default=MIN_FILES)
    ap.add_argument("--fingerprints", default=FINGERPRINTS)
    args = ap.parse_args()

    if not os.path.exists(args.fingerprints):
        sys.exit(f"no fingerprints at {args.fingerprints}; run fingerprint.py")

    rows = load(args.fingerprints)
    print(f"\n  {len(rows)} fingerprints, "
          f"{sum(1 for r in rows if r['balkan'])} in the Balkan cohort")
    found = clusters(rows, run_lengths(rows), args.min_files)

    total = sum(len(c["files"]) for c in found)
    print(f"  {len(found)} shared openings across {total} files\n")
    for c in found:
        print(f"    {len(c['files'])} files, {c['balkan']} Balkan, "
              f"median {c['median_secs']:.2f}s"
              + (f", {c['chained_out']} chained out" if c["chained_out"] else ""))
        for f in c["files"][:6]:
            print(f"       {f['shared_secs']:5.2f}s  {f['file'][:62]}")
        if len(c["files"]) > 6:
            print(f"       ... and {len(c['files']) - 6} more")
        print()

    json.dump({"clusters": found}, open(OUT + ".tmp", "w"),
              ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    print(f"  -> {OUT}\n")
    print("  Nothing is cut from this. Confirm a file in the review sheet\n"
          "  first: eight NCS tracks share an opening beat and none of them\n"
          "  has an intro.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
