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
bumper and some do not. `intro=<seconds>` cuts a length timed by ear instead,
on any file, which is the escape hatch for the openings recall still misses.

What actually comes off is not the shared run. The run stops at the last item
both files agree on, and the item spanning the boundary disagrees on both
sides, so it undershoots by construction and cutting there leaves a fragment
behind. song_start() snaps to the silent gap the bumper hands over across
where there is one, and falls back to the run where the bumper segues straight
into the first bar. write_tags does the cutting; redownload.py --intros looks
for a clean copy first, which is better than cutting when one exists.

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
import re
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

# How far from the fingerprint's boundary a silent gap may sit and still be
# taken as the handover from the bumper to the song.
SNAP = 1.5
# The gap between a bumper and the first bar is a duck, not digital silence.
SILENCE_DB = -45.0
MIN_GAP = 0.12
# How far past the band the scan runs, so that the window's own edge can never
# be mistaken for a boundary, and how close to that edge an event has to sit
# before it is read as the artifact it is. See song_start().
EDGE_PAD = 1.0
EDGE_EPS = 0.05
# Nothing longer than this is cut, whatever anyone answers. The longest
# confirmed opening here is 7.5s; a number an order of magnitude past that is
# a typo, and a typo that deletes the first verse.
MAX_CUT = 30.0

_SIL_END = re.compile(r"silence_end:\s*([\d.]+)")


def _load(path):
    """-> the JSON at `path`, or {} when it is absent or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def stamp(path):
    """-> (size, mtime) for `path`, or None if it is gone.

    Written next to every proposed cut so a stale answer cannot be applied to
    a file that has since been replaced. That is not hypothetical: the policy
    for a confirmed intro is to fetch a clean copy first, and the clean copy
    lands at the same path under the same name with the bumper already
    absent. Cutting it again would take the first seven seconds of the song.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, int(st.st_mtime)]


def song_start(path, nominal):
    """-> (seconds to cut, whether a gap was found) for one file.

    `nominal` is where the shared opening stops matching, and it undershoots
    by design: the fingerprint item that spans the boundary already disagrees
    on both sides, so it is never counted in the run. Cutting there leaves a
    fragment of the bumper behind.

    Where the bumper hands over across a duck or a beat of quiet, that gap is
    the real boundary and is worth snapping to. Where it segues straight into
    the first bar there is nothing to snap to, and the run is the best number
    anything here has.

    Seconds is None when the scan could not be run at all, which is a
    different answer from "no gap here" and has to stay different. ffmpeg
    missing, a decode that timed out and a source that has gone all come back
    that way, and returning `nominal` for them would file the undershooting
    run as a measurement. Every grouped file would then carry a length nobody
    measured, and `intro=y` would cut it.
    """
    from pipeline import silence
    # Scanned past the far edge of the band, not up to it. `silencedetect`
    # closes any run still open when the input ends, so a window that stops at
    # `nominal + SNAP` reports a silence_end there whenever the file is quiet
    # at that moment, and that number is where we stopped looking rather than
    # where the bumper handed over. It sat exactly on the band's edge, so it
    # was always accepted and always won: on this library it fired on 13 of
    # the 106 grouped files, moving every one of them by the full 1.5s.
    #
    # This is #62's scan-window bug at the other end of the file, which is why
    # the guard is here twice: the window is widened so a real boundary can
    # never coincide with it, and an event that lands on it anyway is dropped
    # rather than trusted.
    window = nominal + SNAP + EDGE_PAD
    err = silence.ffmpeg(["-t", f"{window:.2f}", "-i", path, "-af",
                          f"silencedetect=noise={SILENCE_DB}dB:d={MIN_GAP}"])
    if err is None:
        return None, False               # could not look, so nothing is known
    if not err:
        return nominal, False
    near = [e for e in (float(m.group(1)) for m in _SIL_END.finditer(err))
            if abs(e - nominal) <= SNAP and window - e > EDGE_EPS]
    if not near:
        return nominal, False
    return round(min(near, key=lambda e: abs(e - nominal)), 3), True


def answers():
    """-> {basename: True|False|seconds} from hints.tsv.

    Read here rather than in write_tags, for the reason silence.hinted_trims()
    is: one place decides, so the review sheet and the file cannot disagree.
    """
    out = {}
    try:
        from pipeline.review import HINTS, parse_hint
        if not os.path.exists(HINTS):
            return out
        with open(HINTS, encoding="utf-8") as fh:
            next(fh, None)                      # header: file, hint
            for line in fh:
                name, _, hint = line.rstrip("\n").partition("\t")
                kind, payload = parse_hint(hint)
                if kind == "intro":
                    out[name] = payload
    except Exception:
        pass                                    # a hint is help, never a gate
    return out


def cuts(paths=None, cache_path=None):
    """-> {source path: seconds} for every file confirmed to open with a bumper.

    Two ways in, and they are not the same promise.

    `intro=y` takes the length this library measured, and three things have to
    agree before a second of it comes off: a shared opening was measured, you
    confirmed it on that file, and the file on disk is still the one that was
    measured. A group you never answered and a file refetched since both come
    out of here empty.

    `intro=12` is a length you timed yourself. It applies to the file whatever
    the measurement says and whether or not the detector ever grouped it,
    which is what makes it the escape hatch for the openings recall still
    misses. The cost of that is the one case the stamp would have caught: fetch
    a clean copy of a file you hand-timed and the number still applies to the
    replacement, so answer `intro=n` once the new copy is clean.
    """
    said = answers()
    if not said:
        return {}
    out = {}
    blob = _load(cache_path or OUT)
    for cluster in blob.get("clusters") or []:
        for f in cluster["files"]:
            if said.get(f["file"]) is not True:
                continue                        # unanswered, no, or hand-timed
            secs = f.get("intro_cut")
            if not secs or not 0 < secs <= MAX_CUT:
                continue
            if f.get("stamp") != stamp(f["path"]):
                continue                        # replaced since it was measured
            if f.get("changed_since_answered"):
                # Replaced, and then measured again, which puts the stamp back
                # in step and would otherwise re-arm an answer given about the
                # file that is gone. The measurement is current; the answer is
                # not, and only one of the two knows whether the bumper is
                # still there.
                continue
            out[f["path"]] = float(secs)
    for p in paths or ():
        told = said.get(os.path.basename(p))
        if told is not True and told and 0 < told <= MAX_CUT:
            out[p] = float(told)
    return out


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


def propose(found, workers=None):
    """Fill in what would actually be cut from each file, in place.

    Separate from clusters() because it decodes audio: the matrix above is
    arithmetic on fingerprints and runs in a second, and this is one ffmpeg
    per file. Only the boundary matters, so each scan stops a second and a
    half past it rather than reading the whole track.

    Incremental, and that is a correctness property here rather than a saving.
    The stamp `cuts()` checks is written by this function, so re-stamping
    every run would refresh the guard against whatever is on disk now: swap in
    the clean copy that `--intros` fetched, re-run this, and the stamp matches
    again, which re-arms a confirmed `intro=y` against a file whose bumper is
    already gone. That is the case stamp() exists to prevent, and rewriting an
    entry that is already correct is how it was being defeated. A file is
    re-measured only when its stamp has moved or it has no length yet.

    The last run's entries are read back from the cache for that, keyed by
    path, because clusters() rebuilds its dicts from the fingerprints every
    time and the stamp written last time is not in them.
    """
    from concurrent.futures import ThreadPoolExecutor
    files = [f for c in found for f in c["files"]]
    if not files:
        return found
    if workers is None:
        workers = min(8, (os.cpu_count() or 2))
    was = {}
    for c in (_load(OUT).get("clusters") or []):
        for f in c.get("files") or ():
            if f.get("path") and f.get("intro_cut"):
                was[f["path"]] = f

    def one(f):
        now = stamp(f["path"])
        old = was.get(f["path"])
        if old is not None and old.get("stamp") == now:
            # Measured already, on this exact file. Keep the recorded stamp
            # rather than writing an identical one, so the guard keeps
            # pointing at the moment the measurement was actually taken.
            f["stamp"] = old["stamp"]
            f["intro_cut"] = old["intro_cut"]
            f["snapped"] = old.get("snapped", False)
            # And carry the mark forward, which is the whole of it. Setting
            # this flag on the run that notices the file moved is not enough:
            # that run also writes the new stamp, so the NEXT run matches and
            # takes this branch, and a flag that stopped here would be dropped
            # exactly one run after it was raised. The answer would then apply
            # to the replacement after all, which is what the flag exists to
            # prevent. It survives until the file moves again.
            if old.get("changed_since_answered"):
                f["changed_since_answered"] = True
            return
        secs, snapped = song_start(f["path"], f["shared_secs"])
        if secs is None:
            # The scan failed. No length, so cuts() has nothing to accept and
            # a later run will try again.
            f.pop("intro_cut", None)
            f["stamp"], f["snapped"] = now, False
            return
        f["stamp"] = now
        f["intro_cut"] = min(round(secs, 3), MAX_CUT)
        f["snapped"] = snapped
        # Measured before, and the file has moved since. The measurement below
        # is of whatever is there now, but the ANSWER on record was given about
        # what was there before, and the two are not the same question: the
        # policy for a confirmed bumper is to fetch a clean copy and swap it
        # in, so "this file changed" is the expected shape of exactly the case
        # that must not be cut twice. Marked rather than cut, and cuts()
        # refuses it from then on. Not "until you answer again": hints.tsv
        # records the answer and not when it was given, so a second `intro=y`
        # is the same line as the first and nothing here can tell them apart.
        # The escapes are `intro=n`, which is the right answer for the clean
        # copy this case exists to describe, and `intro=<seconds>`, which
        # applies whatever the cluster says. Refusing costs a bumper left in;
        # the other way round costs the first seven seconds of the song.
        if old is not None:
            f["changed_since_answered"] = True

    # Every worker is waiting on an ffmpeg subprocess, so threads are right
    # here for the reason they are in silence.py.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, files))
    return found


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

    found = propose(clusters(rows, run_lengths(rows), args.min_files))
    total = sum(len(c["files"]) for c in found)
    snapped = sum(1 for c in found for f in c["files"] if f.get("snapped"))
    print(f"  {len(found)} shared openings across {total} files, "
          f"{snapped} of them cutting at a gap rather than at the run\n")
    for c in found:
        print(f"    {len(c['files'])} files ({c['distinct_songs']} songs), "
              f"{c['balkan']} Balkan, median {c['median_secs']:.2f}s"
              + (f", {c['chained_out']} chained out" if c["chained_out"] else ""))
        for f in c["files"][:6]:
            print(f"       {f['shared_secs']:5.2f}s shared, cut "
                  f"{f.get('intro_cut', 0):5.2f}s"
                  f"{'*' if f.get('snapped') else ' '}  {f['file'][:48]}")
        if len(c["files"]) > 6:
            print(f"       ... and {len(c['files']) - 6} more")
        print()

    json.dump({"clusters": found}, open(OUT + ".tmp", "w"),
              ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    print(f"  -> {OUT}")
    print("\n  Nothing is cut from this. Each file becomes a review row, and\n"
          "  only an answer turns into a cut: eight NCS tracks share an\n"
          "  opening beat and none of them has an intro. `intro=y` takes the\n"
          "  measured length above, `intro=7.5` takes yours, `intro=n` keeps\n"
          "  the file whole. A `*` means the cut lands on a silent gap.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
