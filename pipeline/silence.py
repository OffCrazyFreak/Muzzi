#!/usr/bin/env python3
"""Stage 5b: measure the dead air at the head of every source file.

Measures only. Nothing here writes audio -- write_tags.py does the cutting,
when it makes its copy, so the number below is always derived from an
untrimmed original. That is what makes trimming idempotent: re-running can
never trim twice, because the thing being measured never changes.

Why it matters: on a random 120-file sample of the library, 48% of files began
with more than 0.05s of silence, 32% with more than 0.2s, 7% with more than a
second and one with 8.3s. On the phone that is a dead beat before every other
song.

Threshold: fixed -50 dBFS, raised to sit 6 dB over the file's own noise floor
when that floor is unusually high. The relative part is a safety net, not the
driver, and the distinction was measured rather than assumed. Peak-relative
thresholds (what librosa's trim does) collapse into a fixed one here: 59 of 60
sampled files peak above -3 dBFS, because modern masters are brickwalled. An
RMS-relative threshold in the EBU R128 style changed the decision on 13% of
files and only marginally. And not one file was found where -50 dB saw no
silence but -40 dB did, which is the case a fixed threshold handles badly. So
the clamp costs one cheap pass and earns its place the day a hissy rip lands
in the library, not today.

Usage:
  silence.py
  silence.py --limit 50
  silence.py --force
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

REVIEW = os.path.join(HERE, "cache", "review.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
OUT = os.path.join(HERE, "cache", "silence.json")

# Below this, silence is silence and the noise floor does not get a vote.
BASE_THRESHOLD_DB = -50.0
# How far over a measured floor the threshold has to sit to clear it.
FLOOR_MARGIN_DB = 6.0
# A "floor" louder than this is not a floor, it is the song. Measured: 94 of
# 280 files open on music rather than silence, and reading that as a noise
# floor produced thresholds as absurd as -1.9 dB -- at which point almost the
# whole track counts as silence. Those files have no leading silence to find,
# so the base threshold is already the right answer for them.
FLOOR_IS_MUSIC_DB = -40.0
# And never let the clamp run past here even so.
MAX_THRESHOLD_DB = -30.0
# Everything under this is inaudible; churning the file would buy nothing.
MIN_TRIM = 0.2
# Over this it is far more likely to be a quiet intro than dead air.
MAX_TRIM = 10.0
# Leave this much silence in front so a soft attack survives the cut.
GUARD = 0.04
# And this much behind. Ten times the head's guard, deliberately: an abrupt cut
# on a decaying last note is far more audible than a late start, and half a
# second of run-off is what a track is expected to end with anyway.
TAIL_GUARD = 0.5
# Below this much reclaimed, remuxing the file buys nothing worth the churn.
# With the guard above, that means a tail is cut from 1.0s of silence up.
TAIL_MIN_GAIN = 0.5
# A silence has to last this long to count, and we look this far in.
MIN_SILENCE = 0.05
SCAN_SECS = 30
FLOOR_SECS = 0.2
# How close to the edge of the scan window counts as touching it. Frame
# granularity and the reverse filter both move the boundary by a few tens of
# milliseconds, so an exact comparison would miss the clipped files it exists
# to catch.
EDGE_TOLERANCE = 0.5

_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*([\d.]+)")
# astats prints one of these per channel and once for "Overall".
_RMS = re.compile(r"RMS level dB:\s*(-?[\d.]+|-?inf)", re.IGNORECASE)


def ffmpeg(args, timeout=120):
    """-> ffmpeg's stderr, or None if it could not be run.

    Same shape as the rest of the repo's subprocess use: never raises, and a
    missing binary or a hung decode comes back as a value, not an exception.

    Public because `intros.py` snaps its cut to a silent gap and there is no
    reason for two spellings of the same three lines.
    """
    try:
        p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats",
                            *args, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    return p.stderr


def noise_floor(path):
    """-> the RMS level in dBFS of the first fraction of a second.

    Digital silence reports -inf, which is the common case and the reason the
    return can be None rather than a number. A file that opens straight onto
    music reports something loud, which is not a floor at all -- measure()
    checks for that before letting this move the threshold.
    """
    err = ffmpeg(["-t", str(FLOOR_SECS), "-i", path, "-af", "astats"])
    if err is None:
        return None
    vals = [m.group(1).lower() for m in _RMS.finditer(err)]
    real = [float(v) for v in vals if "inf" not in v]
    return max(real) if real else None


def leading_silence(path, threshold_db):
    """-> seconds of silence at the very start, 0.0 when the file starts loud.

    silencedetect reports every silent run in the file. Only a run that opens
    at 0 is leading silence; one that opens later is a pause in the music and
    none of our business.
    """
    err = ffmpeg(["-t", str(SCAN_SECS), "-i", path, "-af",
                   f"silencedetect=noise={threshold_db:.1f}dB:d={MIN_SILENCE}"])
    if err is None:
        return None
    start = _SIL_START.search(err)
    if not start or abs(float(start.group(1))) > 0.001:
        return 0.0
    end = _SIL_END.search(err)
    # No end marker means the file was still silent when the scan window ran
    # out. That is a real measurement, not a failure -- it just lands well
    # past the point where anything is cut automatically, so it becomes a
    # question for the review sheet rather than a retry every run.
    return float(end.group(1)) if end else float(SCAN_SECS)


def trailing_silence(path, threshold_db, duration=None):
    """-> seconds of silence at the very end, 0.0 when the file ends loud.

    Two passes, and they use different methods on purpose.

    The fast one decodes only the last `SCAN_SECS`, reverses it with `areverse`
    and reads the run that then opens at 0. Same filter, same thresholds and
    the same shape as the head, so the two numbers are directly comparable,
    which they would not be if the tail were derived from `silence_start`
    markers against a duration read from somewhere else.

    The scan window is the hazard. ffmpeg emits a `silence_end` at the segment
    boundary whether or not the silence really ended there, so a file whose
    dead air outlasts the window reads as exactly one window's worth. Measured:
    without the second pass, four of the ten longest tails in this library
    report 30s instead of their real 30 to 36, and each would be under-cut by
    the difference.

    The second pass scans the whole file forwards instead of reversing it.
    `areverse` has to hold everything it is given in memory, so widening the
    window is paid for in RAM, once per worker: a bounded 30s reversal is about
    10MB, and reversing a whole DJ set across eight threads is not. Forward
    `silencedetect` streams, so the fallback costs time and nothing else. It
    reaches the same answer by a different route, which is worth something on
    its own: this is the method that independently confirmed the fast path on
    all ten of the longest tails.
    """
    def reversed_tail(window):
        err = ffmpeg(["-sseof", f"-{window}", "-i", path, "-af",
                       f"areverse,silencedetect=noise={threshold_db:.1f}dB:"
                       f"d={MIN_SILENCE}"], timeout=300)
        if err is None:
            return None
        start = _SIL_START.search(err)
        if not start or abs(float(start.group(1))) > 0.001:
            return 0.0
        end = _SIL_END.search(err)
        return float(end.group(1)) if end else None

    def forward_tail():
        """The last silent run that reaches the end of the file."""
        err = ffmpeg(["-i", path, "-af",
                       f"silencedetect=noise={threshold_db:.1f}dB:"
                       f"d={MIN_SILENCE}"], timeout=600)
        if err is None or not duration:
            return None
        opened = None
        for m in re.finditer(r"silence_(start|end):\s*(-?[\d.]+)", err):
            if m.group(1) == "start":
                opened = float(m.group(2))
            elif opened is not None:
                # A run that closed before the end is a pause in the music.
                last, opened = (opened, float(m.group(2))), None
                if abs(last[1] - duration) < EDGE_TOLERANCE:
                    return max(0.0, duration - last[0])
        # Still open when the file ended: silence all the way out.
        return max(0.0, duration - opened) if opened is not None else 0.0

    if not duration or duration <= SCAN_SECS:
        got = reversed_tail(SCAN_SECS)
        return got if got is not None else (duration or None)
    got = reversed_tail(SCAN_SECS)
    if got is None or got >= SCAN_SECS - EDGE_TOLERANCE:
        whole = forward_tail()
        if whole is not None:
            return whole
    return got


def hinted_trims():
    """-> {basename: True|False} from hints.tsv, for files you have ruled on.

    Read here rather than in write_tags so that one place decides, and the
    review sheet cannot disagree with what actually happens to the file.
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
                if kind == "trim":
                    out[name] = payload
    except Exception:
        pass                                    # a hint is help, never a gate
    return out


def measure(path, hint=None, duration=None):
    """-> the record cached for one source file.

    `hint` is your answer from sheet 4: False pins the file at full length,
    True releases one whose silence was too long to cut without being asked.
    It governs both ends, because it is an answer about this file rather than
    about one of its edges.

    `duration` lets the tail scan seek instead of decoding the whole file. It
    comes from analyze, and its absence costs time rather than accuracy.
    """
    if not os.path.exists(path):
        return {"path": path, "error": "missing"}
    try:
        floor = noise_floor(path)
        threshold = BASE_THRESHOLD_DB
        if floor is not None and floor <= FLOOR_IS_MUSIC_DB:
            threshold = min(MAX_THRESHOLD_DB,
                            max(BASE_THRESHOLD_DB, floor + FLOOR_MARGIN_DB))
        lead = leading_silence(path, threshold)
        tail = trailing_silence(path, threshold, duration)
    except Exception as e:
        return {"path": path, "error": f"{type(e).__name__}: {str(e)[:150]}"}
    if lead is None:
        return {"path": path, "error": "ffmpeg produced no measurement"}

    rec = {"path": path, "lead": round(lead, 3),
           "noise_floor_db": round(floor, 1) if floor is not None else None,
           "threshold_db": round(threshold, 1)}
    if tail is not None:
        rec["tail"] = round(tail, 3)
    if hint is not None:
        rec["hinted"] = bool(hint)
    if lead <= 0.0:
        rec["decision"] = "no_leading_silence"
    elif hint is False:
        rec["decision"] = "kept_by_you"
    elif lead < MIN_TRIM:
        rec["decision"] = "skip_short"
    elif lead > MAX_TRIM and hint is not True:
        rec["decision"] = "review_long"
    else:
        rec["decision"] = "trim"
        rec["cut"] = round(max(0.0, lead - GUARD), 3)

    # The tail decides separately. A quiet intro is usually a quiet intro,
    # which is why the head sends anything over ten seconds to be looked at;
    # a quiet outro is dead air, verified digital silence in every one of the
    # longest cases in this library, so there is no ceiling here. What guards
    # the tail instead is the lyric check in write_tags: a cut that would
    # remove audio a synced sheet still has words for is a contradiction, and
    # contradictions are not resolved by cutting.
    if tail is None:
        rec["tail_decision"] = "unmeasured"
    elif tail <= 0.0:
        rec["tail_decision"] = "no_trailing_silence"
    elif hint is False:
        rec["tail_decision"] = "kept_by_you"
    elif tail - TAIL_GUARD < TAIL_MIN_GAIN:
        rec["tail_decision"] = "skip_short"
    else:
        rec["tail_decision"] = "trim"
        rec["tail_cut"] = round(max(0.0, tail - TAIL_GUARD), 3)
    return rec


def cut_for(rec):
    """-> how much to take off the front, or 0.0 to copy it untouched.

    The one place the decision is read. Anything that is not a clean `trim`
    verdict -- an error, a long intro waiting on review, a file measured by an
    older version of this stage -- copies whole.
    """
    if not isinstance(rec, dict) or rec.get("decision") != "trim":
        return 0.0
    return float(rec.get("cut") or 0.0)


def tail_cut_for(rec):
    """-> how much to take off the end, or 0.0 to copy it whole.

    Same rule as `cut_for`, and separate from it on purpose: a record written
    before this stage measured tails has no `tail_decision` at all, and must
    keep its whole ending rather than inherit the head's verdict.
    """
    if not isinstance(rec, dict) or rec.get("tail_decision") != "trim":
        return 0.0
    return float(rec.get("tail_cut") or 0.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("-j", "--workers", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # This stage runs beside `analyze`, before the first `review`, so on a
    # first run review.json does not exist yet. Raising here would take the
    # whole chain down with it and `dedupe` would never run, which is how a
    # duplicate copy reaches the output. All that is needed is a list of
    # source paths, and analyze has just written one.
    if os.path.exists(REVIEW):
        rows = json.load(open(REVIEW))
    elif os.path.exists(ANALYSIS):
        rows = [v for v in json.load(open(ANALYSIS)).values() if v.get("path")]
    else:
        print("  no track list yet: run fingerprint and analyze first\n")
        return 0
    done = {} if args.force else (json.load(open(OUT))
                                  if os.path.exists(OUT) else {})

    paths, seen = [], set()
    for r in rows:
        p = r.get("path")
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    # Only to let the tail scan seek to the end instead of decoding the whole
    # file. A path analyze has not reached yet is measured the slow way rather
    # than skipped.
    secs = {}
    if os.path.exists(ANALYSIS):
        for v in json.load(open(ANALYSIS)).values():
            if v.get("path") and v.get("decoded_secs"):
                secs[v["path"]] = v["decoded_secs"]

    hints = hinted_trims()
    # A record from before this stage measured tails has no `tail_decision`,
    # and re-measuring it is the only way it ever gets one. Without this the
    # whole library keeps its dead air until someone passes --force.
    todo = [p for p in paths
            if p not in done
            or "tail_decision" not in done[p]
            or done[p].get("hinted") != hints.get(os.path.basename(p))]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"  nothing new to measure ({len(done)} known)\n")
        return 0

    # Every worker is waiting on an ffmpeg subprocess, so threads are right
    # here; the CPU-bound stages use processes for a reason that does not
    # apply.
    workers = args.workers or min(8, (os.cpu_count() or 4))
    print(f"  {len(todo)} of {len(paths)} files to measure, {workers} workers\n")
    lock, n, t0 = threading.Lock(), [0], time.time()
    failed = Counter()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(measure, p, hints.get(os.path.basename(p)),
                             secs.get(p))
                   for p in todo]
        for f in as_completed(futures):
            try:
                rec = f.result()
            except Exception:
                continue
            with lock:
                n[0] += 1
                # An error is never cached. A source that is missing today may
                # be back after the next re-download, and a cached failure
                # would mean no run ever looks at it again -- the exact bug
                # lyrics_fetch.py was written to undo.
                if rec.get("error"):
                    failed[rec["error"].split(":")[0]] += 1
                else:
                    done[rec["path"]] = rec
                if n[0] % 100 == 0:
                    el = time.time() - t0
                    print(f"    {n[0]}/{len(todo)}  {el:.0f}s elapsed, "
                          f"eta {el/n[0]*(len(todo)-n[0]):.0f}s", flush=True)
                    json.dump(done, open(OUT + ".tmp", "w"), indent=1)
                    os.replace(OUT + ".tmp", OUT)

    json.dump(done, open(OUT + ".tmp", "w"), indent=1)
    os.replace(OUT + ".tmp", OUT)

    verdicts = Counter(r.get("decision") for r in done.values())
    trims = [r["cut"] for r in done.values() if r.get("decision") == "trim"]
    print(f"\n  measured {n[0]} files in {time.time()-t0:.0f}s\n")
    for k in ("trim", "skip_short", "review_long", "kept_by_you",
              "no_leading_silence"):
        if verdicts.get(k):
            print(f"    {k:20s} {verdicts[k]:5}")
    for k, c in sorted(failed.items()):
        print(f"    {('not measured: ' + k)[:20]:20s} {c:5}  (will retry)")
    if trims:
        trims.sort()
        print(f"\n    to be cut: {len(trims)} files, "
              f"median {trims[len(trims)//2]:.2f}s, longest {trims[-1]:.2f}s")
    print(f"\n  -> {OUT}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
