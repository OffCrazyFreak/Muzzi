#!/usr/bin/env python3
"""Stage 6c: measure how far each synced lyric sheet is out of step with its
audio, so that trimming can be applied without guessing.

This exists because of a measurement, not a theory. A text-anchored Whisper
pass over 60 files found a median error of +0.97s, with half the tracks out by
more than a second. The sign matters: positive means the audio lags the sheet.
Mean leading silence over the same files was 0.61-0.86s. Those two numbers
agreeing in sign and size says what is going on:

  LRCLIB sheets are timed against the commercial master. A YouTube rip carries
  extra padding at the head. Cutting that padding moves the file ONTO the
  sheet's timeline, not off it.

Which is why write_tags shifts by `offset - cut` and not by `-cut`. Trimming a
file whose whole error was leading silence needs no shift at all, and shifting
it anyway would double the error on precisely the tracks this is meant to fix.

Method, per track:

  1. take the first few lyric lines with enough distinct words to be findable
  2. transcribe from 0 with word timestamps
  3. slide a window over the transcript and score token overlap, fuzzily,
     because Whisper writes "obcajna" where the sheet has "očajna"
  4. take the EARLIEST window within tolerance of the best score

Step 4 is not a detail. Taking the best-scoring window anywhere in the clip let
a repeated chorus line match a later occurrence, which is how an exploratory
version of this produced offsets of +81s and -54s at full confidence.

Tracks whose file length disagrees with the matched LRCLIB duration by more
than 10s are not transcribed at all. Their sheet belongs to a different
recording -- a live cut, an extended mix -- and no single offset can fix that,
so they are recorded for the review sheet instead.

Usage:
  lyric_align.py
  lyric_align.py --limit 30
  lyric_align.py --model small
  lyric_align.py --force
"""
import argparse
import difflib
import hashlib
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import lrc  # noqa: E402
from pipeline import transcribe  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
LYRICS = os.path.join(HERE, "cache", "lyrics.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
OUT = os.path.join(HERE, "cache", "lyric_align.json")
MODELS = os.path.join(HERE, "models")

# A word shorter than this carries no matching power ("a", "the", "je").
MIN_WORD = 3
# Fewer distinct words than this and the anchor matches half the song.
MIN_ANCHOR_WORDS = 3
# How much of the anchor has to be found before the match is believed.
MIN_CONFIDENCE = 0.5
# Two windows this close in score are a tie, and a tie goes to the earlier one.
SCORE_TOLERANCE = 0.05
# Beyond this an "offset" is a mismatch, not a timing error. Tied to the
# largest silence silence.py will cut on its own: an error bigger than that
# cannot be explained by our own trimming, and a sheet genuinely that far out
# is a wrong-version problem, which the duration gate below already catches.
# Measured why this matters: at 30s, "A Great Big World - Say Something" asked
# for a +23s shift, which is its long piano intro fooling the anchor, not a
# real error -- and applying it would have wrecked a usable sheet.
MAX_PLAUSIBLE_OFFSET = 10.0
# Beyond this the sheet is for a different recording entirely.
WRONG_RECORDING_DELTA = 10.0
# How far past the last anchor to listen. This is the dominant cost of the
# stage, so it is as small as it can be while still covering a file that lags
# its sheet by a lot.
LOOKAHEAD = 45.0

_MODEL = None


def norm_words(text):
    """Whisper-tolerant tokenisation, shared with verify_lyrics.

    Imported on use rather than at module scope on purpose. write_tags reads
    offset_for() from here, and four other stages import write_tags for
    split_credits -- a module-level import would drag verify_lyrics and its
    network dependencies into all of them for a six-line function.
    """
    from pipeline.verify_lyrics import norm_words as _impl
    return _impl(text)


def _model(name, threads, root):
    """Lazy per-process load, same as verify_lyrics: once per worker, never
    once per track."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(name, device="cpu", compute_type="int8",
                              download_root=root, cpu_threads=threads)
    return _MODEL


def anchors_for(synced):
    """-> [(seconds, [tokens])] for the first few findable lines.

    Findable means enough distinct words long enough to be worth matching.
    Several are returned, not one: Whisper mangles sung words badly enough
    that any single line can fail to match even when the audio is exactly
    what the sheet says. Trying four costs nothing extra -- they are all
    scored against one transcription -- and lifted the measurement rate from
    27% to something usable.

    Only lines near the start are considered. The anchor exists to locate the
    beginning of the singing, and a line from the third chorus would be both
    slower to reach and likelier to repeat.
    """
    out = []
    for t, words in lrc.lines(synced)[:8]:
        toks = [w for w in dict.fromkeys(norm_words(words)) if len(w) >= MIN_WORD]
        if len(toks) >= MIN_ANCHOR_WORDS:
            out.append((t, toks))
        if len(out) >= 4:
            break
    return out


def _present(token, window):
    """-> True when `token` is in `window`, allowing for Whisper's spelling.

    Exact first, because it is the common case and cheap. The fuzzy fallback
    is what catches "obcajna" for "očajna" and the endless small mishearings
    of sung consonants; below 0.8 the words are simply different.
    """
    if token in window:
        return True
    return any(difflib.SequenceMatcher(None, token, w).ratio() >= 0.8
               for w in window)


def locate(words, anchor):
    """-> (start_seconds, score) for the earliest good match of `anchor`.

    `words` is [(start, token)] in time order. The window is the anchor's own
    length plus a little slack, because Whisper drops and invents words and an
    exact-length window would punish both.

    Earliest, not best. Taking the best-scoring window anywhere in the clip is
    what let a repeated chorus line match its second occurrence and report an
    offset of +81s at full confidence.
    """
    n = len(anchor)
    if not words or not n:
        return None, 0.0
    span = n + 2
    scored = []
    for i in range(len(words)):
        window = {w for _, w in words[i:i + span]}
        hit = sum(1 for tok in anchor if _present(tok, window))
        scored.append((hit / n, words[i][0]))
    best = max(s for s, _ in scored)
    if best <= 0:
        return None, 0.0
    for score, start in scored:
        if score >= best - SCORE_TOLERANCE:
            return start, best
    return None, 0.0


def align_one(task):
    """(path, synced, model_name, threads, root) -> partial record.

    Runs in a worker process.
    """
    path, synced, model_name, threads, root = task
    try:
        cands = anchors_for(synced)
        if not cands:
            return path, {"status": "no_anchor", "offset": None}

        model = _model(model_name, threads, root)
        # Listen from the start to a little past the last anchor. Transcribing
        # further is the single biggest cost here and buys nothing: every
        # anchor lies inside this window by construction.
        end = max(t for t, _ in cands) + LOOKAHEAD
        def collect(segs):
            got = []
            for seg in segs:
                for w in (seg.words or []):
                    tok = norm_words(w.word)
                    if tok:
                        got.append((float(w.start), tok[0]))
            return got, len(got)

        # The voice-activity filter hands Whisper silence on a lot of sung
        # audio, and it does so worst on the half of this library it is
        # hardest to identify anyway. transcribe.listen drops it and asks
        # again when the answer is implausibly thin.
        words, _info, used_vad = transcribe.listen(
            model, path, collect, beam_size=1, word_timestamps=True,
            condition_on_previous_text=False, clip_timestamps=[0, end])
        if len(words) < MIN_ANCHOR_WORDS:
            return path, {"status": "nothing_transcribed", "offset": None,
                          "vad": used_vad}

        # Best-scoring anchor wins, but each anchor is still located by its
        # own earliest acceptable window.
        best = (None, 0.0, None, None)
        for lrc_t, anchor in cands:
            start, score = locate(words, anchor)
            if start is not None and score > best[1]:
                best = (start, score, lrc_t, anchor)
        start, score, lrc_t, anchor = best
        if anchor is None:
            lrc_t, anchor = cands[0]

        rec = {"confidence": round(score, 2),
               # Whether the filter was kept. A run where many measurements
               # needed it dropped is a run that would have measured nothing
               # before, which is worth being able to count.
               "vad": used_vad,
               "anchor": " ".join(anchor)[:60],
               "anchor_time": round(lrc_t, 2),
               "anchors_tried": len(cands)}
        if start is None or score < MIN_CONFIDENCE:
            rec.update(status="low_confidence", offset=None)
            return path, rec
        offset = start - lrc_t
        if abs(offset) > MAX_PLAUSIBLE_OFFSET:
            rec.update(status="implausible", offset=None,
                       raw_offset=round(offset, 2))
            return path, rec
        rec.update(status="measured", offset=round(offset, 2),
                   matched_time=round(start, 2))
        return path, rec
    except Exception as e:
        return path, {"status": f"error: {type(e).__name__}: {str(e)[:80]}",
                      "offset": None}


def _worker_default():
    """Whisper base int8 holds roughly 0.5GB resident per process."""
    cores = max(1, (os.cpu_count() or 4) - 2)
    try:
        with open("/proc/meminfo") as fh:
            total_kb = next(int(l.split()[1]) for l in fh
                            if l.startswith("MemTotal"))
        by_ram = max(1, int((total_kb / 1024 / 1024 - 3) // 0.7))
        return max(1, min(cores, by_ram, 8))
    except Exception:
        return min(cores, 4)


def sheet_digest(synced):
    """-> a short fingerprint of the lyric body an offset was measured against.

    An offset only means anything next to the sheet it was measured from. When
    lyrics_fetch re-judges a track and swaps in a different sheet, the cached
    number silently starts describing the wrong thing -- and the cache is keyed
    by file path, so nothing else would notice.

    Digesting the body rather than watching a selector version or the `matched`
    name is what makes this exact in both directions: a re-judge that keeps the
    same sheet costs nothing, and a changed sheet is caught even if the matched
    name and the picker version both stayed put.
    """
    if not synced:
        return None
    return hashlib.sha1(synced.encode("utf-8")).hexdigest()[:16]


def offset_for(rec, synced=None):
    """-> the measured offset in seconds, or None when there isn't a trusted
    one. The single place the rest of the pipeline reads this cache.

    Pass the sheet that is about to be written and a measurement taken against
    a different one is refused, so a stale offset cannot be applied even if
    this stage has not re-run since the sheet changed.
    """
    if not isinstance(rec, dict) or rec.get("status") != "measured":
        return None
    if synced is not None and rec.get("sheet") != sheet_digest(synced):
        return None
    off = rec.get("offset")
    return float(off) if off is not None else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("-j", "--workers", type=int)
    ap.add_argument("--model", default="base",
                    choices=["tiny", "base", "small", "medium"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    rows = json.load(open(REVIEW))
    lyrics = json.load(open(LYRICS)) if os.path.exists(LYRICS) else {}
    done = {} if args.force else (json.load(open(OUT))
                                  if os.path.exists(OUT) else {})

    durations = {}
    if os.path.exists(ANALYSIS):
        for v in json.load(open(ANALYSIS)).values():
            if v.get("path") and v.get("decoded_secs"):
                durations[v["path"]] = float(v["decoded_secs"])

    # Phase 1: pair every track with its sheet, and settle the cheap question
    # first. A duration that disagrees by more than ten seconds means the sheet
    # is for another recording, and no amount of transcription changes that.
    tasks, skipped = [], Counter()
    for r in rows:
        path = r.get("path")
        if not path:
            continue
        artist, title = r.get("proposed_artist"), r.get("proposed_title")
        if not (artist and title):
            continue
        entry = lyrics.get(f"{artist}|{title}".lower())
        if not isinstance(entry, dict) or not entry.get("synced"):
            continue
        digest = sheet_digest(entry["synced"])
        prior = done.get(path)
        if prior is not None and prior.get("sheet") == digest:
            continue
        rec = {"path": path, "sheet": digest}
        md, fd = entry.get("matched_duration"), durations.get(path)
        if md and fd:
            rec["duration_delta"] = round(fd - float(md), 1)
            if abs(rec["duration_delta"]) > WRONG_RECORDING_DELTA:
                rec.update(status="wrong_recording", offset=None)
                done[path] = rec
                skipped["wrong_recording"] += 1
                continue
        if not os.path.exists(path):
            skipped["source missing"] += 1
            continue
        tasks.append((path, entry["synced"], rec))

    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        _save(done)
        print(f"  nothing new to align ({len(done)} known)\n")
        return 0

    workers = args.workers or _worker_default()
    per = max(1, (os.cpu_count() or 4) // workers)
    print(f"  {len(tasks)} tracks to align, {workers} workers x {per} threads, "
          f"model {args.model}")
    for k, c in sorted(skipped.items()):
        print(f"    skipped {c} ({k})")
    print()

    pending = {t[0]: t[2] for t in tasks}
    n, t0 = 0, time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(align_one, (p, s, args.model, per, MODELS))
                   for p, s, _ in tasks]
        for f in as_completed(futures):
            try:
                path, part = f.result()
            except Exception:
                continue
            n += 1
            rec = pending.get(path, {"path": path})
            rec.update(part)
            done[path] = rec
            if n % 25 == 0:
                el = time.time() - t0
                print(f"    {n}/{len(tasks)}  {el:.0f}s elapsed, "
                      f"eta {el/n*(len(tasks)-n):.0f}s", flush=True)
                _save(done)

    _save(done)
    stats = Counter(r.get("status") for r in done.values())
    offs = sorted(r["offset"] for r in done.values()
                  if r.get("status") == "measured" and r.get("offset") is not None)
    print(f"\n  aligned {n} tracks in {time.time()-t0:.0f}s\n")
    for k, c in stats.most_common():
        print(f"    {str(k):22s} {c:5}")
    if offs:
        m = offs[len(offs) // 2]
        over = sum(1 for o in offs if abs(o) > 1.0)
        print(f"\n    median offset {m:+.2f}s, "
              f"{over}/{len(offs)} out by more than 1s")
    print(f"\n  -> {OUT}\n")
    return 0


def _save(done):
    json.dump(done, open(OUT + ".tmp", "w"), indent=1, ensure_ascii=False)
    os.replace(OUT + ".tmp", OUT)


if __name__ == "__main__":
    sys.exit(main())
