#!/usr/bin/env python3
"""Files that open with the same recording: label bumpers and producer tags.

Some downloads carry a spoken or musical intro before the song. IDJVideos is
the best known here and it is not the only one: four distinct openings have
been confirmed by ear across this library, on different labels.

Measures and proposes. Nothing here writes audio, and nothing is ever cut on
detection alone.

Why confirmation is not optional
--------------------------------

A shared opening proves two files begin with the same audio. It does not prove
that audio is a bumper. Eight NCS tracks here share up to 8.5s of opening and
the owner confirmed by ear that **none of them has an intro**: dance tracks
built on the same intro beat are indistinguishable from a shared recording by
any measure available here. Cutting them would have removed the first bars of
eight songs.

So a cluster is a question, not a verdict. Answering `intro=y` on a file cuts
that file; answering it on one file of a group says nothing about the others,
because the owner also confirmed that some songs by the same artists carry the
bumper and some do not.

Three things the measurement has to get right, each learned from a failure:

  run, not ratio   scoring overall bit agreement across the opening found a
                   34-file "cluster" whose members shared no single item.
                   Chromaprint bits are not uniformly distributed, so two
                   unrelated quiet openings agree on 92% of bits.
  no chaining      single-link clustering joins A to C through B even when A
                   and C share nothing. Two NCS files were pulled into a group
                   they share 0.00s with.
  not duplicates   three copies of one song share their whole opening and are
                   not a bumper. Files the dedupe stages already call the same
                   song are collapsed to one before a group is counted.

Usage:
  intros.py                 # what shares an opening
  intros.py --min-files 4
  intros.py --tune          # score the thresholds against confirmed answers
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

FINGERPRINTS = os.path.join(HERE, "cache", "fingerprints.json")
DUPES = os.path.join(HERE, "cache", "duplicates.json")
NAME_DUPES = os.path.join(HERE, "cache", "name_duplicates.json")
OUT = os.path.join(HERE, "cache", "intros.json")

# How far into each file to compare. About 11s at this library's item rate,
# comfortably past the longest confirmed intro (7.5s), so a boundary is never
# clipped by the window itself.
ITEMS = 48
# A fingerprint item is 32 bits. Two encodes of one bumper differ by a few bits
# per item; 32 would match everything.
#
# Tuned with --tune against the openings confirmed by ear, and the table has no
# clean winner:
#
#   bits=4 run=12   4 of 5 confirmed groups found, NCS correctly excluded
#   bits=6 run=14   5 of 5 found, NCS admitted
#
# Recall wins, because the two errors do not cost the same. A bumper this
# misses is never offered and never cut, and nothing else in the pipeline will
# find it. A group it admits wrongly costs one keystroke: the NCS files are
# answered intro=n once and never asked about again. Precision would be the
# right preference if detection cut anything by itself, and it does not.
ITEM_BITS = 6
# The shortest run that counts. Under about 2.5s a match is as likely to be a
# common drum fill as a recording.
MIN_RUN = 14
# Fewer distinct songs than this is a coincidence, not a label's bumper.
MIN_FILES = 3


def load(path=None):
    """-> [{path, balkan, secs_per_item, items}] for every usable fingerprint."""
    import chromaprint
    import numpy as np
    rows = []
    for v in json.load(open(path or FINGERPRINTS)).values():
        if not v.get("fingerprint") or not v.get("path"):
            continue
        try:
            raw, _ = chromaprint.decode_fingerprint(v["fingerprint"].encode())
        except Exception:
            continue
        if not raw or len(raw) < ITEMS:
            continue
        rows.append({"path": v["path"], "balkan": bool(v.get("likely_balkan")),
                     "secs_per_item": (v.get("duration") or 1) / len(raw),
                     "items": np.array(raw[:ITEMS], dtype=np.uint32)})
    return rows


def same_song(paths):
    """-> {path: group id} for files the dedupe stages call one song.

    Read from the caches rather than recomputed. Three copies of one recording
    share their whole opening, which is not a bumper and would otherwise be the
    tidiest cluster in the report.
    """
    groups, gid = {}, 0
    for cache, key in ((DUPES, None), (NAME_DUPES, None)):
        if not os.path.exists(cache):
            continue
        blob = json.load(open(cache))
        entries = blob if isinstance(blob, list) else blob.values()
        for g in entries:
            if not isinstance(g, dict):
                continue
            members = [g.get("keep")] + list(g.get("drop") or [])
            members = [m for m in members if m]
            if len(members) < 2:
                continue
            gid += 1
            for m in members:
                groups.setdefault(m, gid)
    for p in paths:
        groups.setdefault(p, None)
    return groups


def run_lengths(rows, item_bits=None):
    """-> an n x n matrix of how many opening items each pair shares."""
    import numpy as np
    bits = ITEM_BITS if item_bits is None else item_bits
    M = np.stack([r["items"] for r in rows])
    n = len(rows)
    run = np.zeros((n, n), dtype=np.int16)
    alive = np.ones((n, n), dtype=bool)
    for k in range(ITEMS):
        col = M[:, k]
        x = col[:, None] ^ col[None, :]
        counted = np.zeros_like(x, dtype=np.uint8)
        t = x.copy()
        while t.any():
            counted += (t & 1).astype(np.uint8)
            t >>= 1
        alive &= counted <= bits
        run += alive
    return run


def clusters(rows, run, min_files=MIN_FILES, min_run=None):
    """-> [{files: [...], ...}] worth asking about.

    Grown by union-find, then pruned twice: every member must share `min_run`
    with the representative directly rather than through a chain, and copies of
    one song count once towards the size of the group.
    """
    import numpy as np
    floor = MIN_RUN if min_run is None else min_run
    n = len(rows)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    ii, jj = np.nonzero(run >= floor)
    for a, b in zip(ii, jj):
        if a < b:
            ra, rb = find(int(a)), find(int(b))
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

    grouped = defaultdict(list)
    for i in range(n):
        grouped[find(i)].append(i)

    dupe_of = same_song([r["path"] for r in rows])
    per_item = float(np.median([r["secs_per_item"] for r in rows]))
    out = []
    for members in grouped.values():
        if len(members) < min_files:
            continue

        def agreement(i, ms=members):
            return float(np.median([run[i][j] for j in ms if j != i]))

        rep = max(members, key=agreement)
        kept = [i for i in members if i == rep or run[rep][i] >= floor]
        # One vote per song. Three copies of one recording are one piece of
        # evidence that an opening recurs, not three.
        songs = {dupe_of.get(rows[i]["path"]) or rows[i]["path"] for i in kept}
        if len(songs) < min_files:
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
                    "distinct_songs": len(songs),
                    "chained_out": len(members) - len(kept),
                    "balkan": sum(1 for f in files if f["balkan"]),
                    "median_secs": round(float(np.median(
                        [f["shared_secs"] for f in files])), 2)})
    return sorted(out, key=lambda c: -len(c["files"]))


# What the owner confirmed by ear. The tuner scores against this, so a
# threshold change that loses a real bumper or admits the NCS beat is visible
# rather than a matter of taste. Two names per group is enough to identify it.
CONFIRMED = [
    ("corona/rimski", ["CORONA - PREDSEDNICKA", "RIMSKI X CORONA - SICILIJA"]),
    ("connect/cvija", ["CONNECT - GETO DJEVOJKA", "CVIJA X TEODORA - NOKAUT"]),
    ("relja", ["RELJA - MARIA", "RELJA X RASTA - GENGE"]),
    ("grse/ttm", ["GRŠE - HIGHLIFE", "TTM - LAGAN SAM"]),
    ("coby/goca/rasta", ["Coby x Senidah - 4 Strane", "Rasta - Kawasaki"]),
]
# Confirmed NOT an intro: a shared opening beat, not a recording.
REFUTED = [("ncs", ["Main Reaktor - Alone", "Ash O'Connor - You"])]


def score(found):
    """-> (kept, missed, refuted_hit, biggest) against the confirmed answers."""
    names = [[f["file"] for f in c["files"]] for c in found]
    kept = []
    for label, wanted in CONFIRMED:
        if any(all(any(w in n for n in grp) for w in wanted) for grp in names):
            kept.append(label)
    bad = [label for label, wanted in REFUTED
           if any(all(any(w in n for n in grp) for w in wanted)
                  for grp in names)]
    return kept, [c for c, _ in CONFIRMED if c not in kept], bad, \
        max((len(g) for g in names), default=0)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-files", type=int, default=MIN_FILES)
    ap.add_argument("--fingerprints", default=FINGERPRINTS)
    ap.add_argument("--tune", action="store_true",
                    help="score thresholds against the confirmed answers")
    args = ap.parse_args()

    if not os.path.exists(args.fingerprints):
        sys.exit(f"no fingerprints at {args.fingerprints}; run fingerprint.py")
    rows = load(args.fingerprints)
    print(f"\n  {len(rows)} fingerprints, "
          f"{sum(1 for r in rows if r['balkan'])} in the Balkan cohort\n")

    if args.tune:
        for bits in (2, 3, 4, 5, 6):
            run = run_lengths(rows, bits)
            for min_run in (12, 14, 16, 20):
                found = clusters(rows, run, args.min_files, min_run)
                kept, missed, bad, biggest = score(found)
                print(f"    bits={bits} run={min_run:<3} groups={len(found):<3}"
                      f" files={sum(len(c['files']) for c in found):<4}"
                      f" biggest={biggest:<3} confirmed={len(kept)}/5"
                      f" NCS={'ADMITTED' if bad else 'excluded'}"
                      + (f"  missing: {', '.join(missed)}" if missed else ""))
        print()
        return 0

    found = clusters(rows, run_lengths(rows), args.min_files)
    total = sum(len(c["files"]) for c in found)
    print(f"  {len(found)} shared openings across {total} files\n")
    for c in found:
        print(f"    {len(c['files'])} files ({c['distinct_songs']} songs), "
              f"{c['balkan']} Balkan, median {c['median_secs']:.2f}s"
              + (f", {c['chained_out']} chained out" if c["chained_out"] else ""))
        for f in c["files"][:6]:
            print(f"       {f['shared_secs']:5.2f}s  {f['file'][:62]}")
        if len(c["files"]) > 6:
            print(f"       ... and {len(c['files']) - 6} more")
        print()

    json.dump({"clusters": found}, open(OUT + ".tmp", "w"),
              ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    print(f"  -> {OUT}")
    print("\n  Nothing is cut from this. Each file becomes a review row, and\n"
          "  only an answer turns into a cut: eight NCS tracks share an\n"
          "  opening beat and none of them has an intro.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
