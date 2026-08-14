#!/usr/bin/env python3
"""Stage 4d: confirm or reject a proposed match by listening to the track.

Every other stage compares *names*. This one compares the actual audio against
what the proposed song is supposed to sound like:

  1. fetch the expected lyrics for the proposed artist/title (LRCLIB, free)
  2. transcribe a 45s excerpt of the real file (faster-whisper small, int8)
  3. measure how much of the transcript appears in those lyrics

Whisper on sung audio is noisy -- roughly "I'm just paranoid" out of Green Day's
Basket Case -- so this can never identify a song from scratch. But noisy text is
plenty to answer "is this the song we think it is?", which is the question that
actually matters when the cost of a wrong tag is high.

Deliberately excerpt-based: 45s from 20% in (past the intro, into the first
verse/chorus). Transcribing whole files would cost days and add nothing.

As a side effect this yields a reliable per-track language, which text-based
detection on a short title cannot.

Usage: verify_lyrics.py [--limit N] [--model small] [--control]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import (ProcessPoolExecutor, ThreadPoolExecutor,
                                as_completed)

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import lyrics_fetch  # noqa: E402
from pipeline import transcribe  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
OUT = os.path.join(HERE, "cache", "lyric_verify.json")
LYRIC_CACHE = os.path.join(HERE, "cache", "lyrics.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
MODELS = os.path.join(HERE, "models")

LRCLIB = "https://lrclib.net/api/search"


_LRC_TS = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")


def _digest(text):
    """-> a short stable fingerprint of the sheet a score was computed from.

    Stored beside the score so a later run can tell whether it is still
    judging the same words. Without it this cache was keyed on the path
    alone, so when lyrics_fetch swapped in a different sheet the old score
    stayed and read as a verification of words we no longer held.
    """
    text = (text or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def _stale(rec, current):
    """-> True when this entry was scored against different words.

    Entries written before the digest existed carry no record of what they
    judged, so they fall back to the coarsest comparison that is still
    honest: whether there were words at all. Treating every legacy entry as
    stale would spend hours of Whisper re-deriving answers that are mostly
    still correct, and the cases that actually changed are exactly the ones
    where presence changed.
    """
    stored = rec.get("lyric_digest")
    if stored is not None:
        return stored != _digest(current)
    return bool(rec.get("have_lyrics")) != bool(current)


def _strip_lrc(text):
    """Drop [mm:ss.xx] stamps so synced lyrics can be compared as plain text."""
    return _LRC_TS.sub("", text).strip() if text else None


def norm_words(text):
    """Lowercase, strip accents and punctuation -> list of words.

    Diacritics must go: Whisper writes "obcajna" where the lyric sheet has
    "očajna", and that should not count as a mismatch.
    """
    if not text:
        return []
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.findall(r"[a-z0-9]+", t)


def ngrams(words, n=2):
    """Word bigrams. Single words match by luck; bigrams need real agreement."""
    if len(words) < n:
        return set(words)
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def containment(transcript, lyrics):
    """Fraction of the transcript's bigrams that appear in the lyrics.

    Containment, not Jaccard: we transcribe 45s against a full lyric sheet, so
    the sets are wildly different sizes and Jaccard would always look terrible.
    """
    t = ngrams(norm_words(transcript))
    l = ngrams(norm_words(lyrics))
    if not t or not l:
        return 0.0
    return len(t & l) / len(t)


# fetch_lyrics() lived here. Superseded by pipeline/lyrics_fetch.py, which
# retries, rate-limits, and never caches a failed request as "no lyrics".


_MODEL = None


def _model(name, threads, root):
    """Lazy per-process load. The model is ~140MB on disk and costs a couple of
    seconds to construct, so it must be built once per worker, not per track."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(name, device="cpu", compute_type="int8",
                              download_root=root, cpu_threads=threads)
    return _MODEL


def transcribe_one(task):
    """(path, lyrics, excerpt, model_name, threads, root) -> partial record.

    Runs in a worker process. Whisper is single-threaded per call, so the old
    sequential loop left 7 of 8 cores idle: measured 5 tracks/min against 1919
    to do, about five hours. The work is per-file independent, so a process
    pool is the whole fix.
    """
    path, lyrics, excerpt, model_name, threads, root = task
    try:
        import mutagen
        model = _model(model_name, threads, root)
        mf = mutagen.File(path)
        dur = float(mf.info.length) if mf and mf.info else 0.0
        # A fixed 20% offset sometimes lands on an instrumental break. Let
        # Whisper's own VAD pick speech regions across a wider window instead.
        lo = max(10.0, dur * 0.12)
        hi = min(dur - 2.0, lo + excerpt * 3.0) if dur else lo + 135.0
        def collect(segs):
            picked, budget = [], excerpt
            for seg in segs:
                picked.append(seg.text.strip())
                budget -= max(0.0, (seg.end or 0) - (seg.start or 0))
                if budget <= 0:
                    break
            text = " ".join(picked)
            return text, len(norm_words(text))

        # The voice-activity filter is trained on speech and hands Whisper
        # silence on a lot of sung audio, worst on the Balkan half. That
        # matters most here of all: this transcript is what waives a name
        # mismatch, so a starved one refuses lyrics on exactly the tracks
        # whose names the catalogues disagree about. Dropped and asked again
        # when the answer is implausibly thin.
        transcript, info, used_vad = transcribe.listen(
            model, path, collect, beam_size=1, without_timestamps=False,
            clip_timestamps=[lo, max(hi, lo + excerpt)])
        sc = round(containment(transcript, lyrics), 3)
        # Whisper always returns SOME language, even for silence. On the NCS
        # instrumental tracks it emitted Khmer at 0.35 confidence with an empty
        # transcript, and 49 tracks were filed under "km" on that basis. Only
        # trust a language when there are actual words and reasonable
        # confidence; otherwise record none.
        prob = float(info.language_probability)
        words = len(norm_words(transcript))
        trusted_lang = info.language if (words >= 8 and prob >= 0.60) else None
        return path, {
            "language": trusted_lang,
            "language_raw": info.language,
            "language_prob": round(prob, 2),
            # No words in a 45s vocal-detected excerpt means there was nothing
            # sung. Combined with no lyric sheet existing, that is an
            # instrumental rather than a failure to transcribe.
            "instrumental": words < 3,
            # Whether the filter was kept. An entry recorded without it is one
            # that would have been silent before, so a run can count how much
            # of its evidence the old setting was throwing away.
            "vad": used_vad,
            "transcript": transcript[:300],
            "score": sc,
            # Confirm on strong overlap, but never auto-REJECT on a low score:
            # an instrumental passage looks identical to a wrong song here.
            "verdict": ("confirmed" if sc >= 0.12 else
                        "weak" if sc >= 0.04 else "inconclusive"),
        }
    except Exception as e:
        return path, {"verdict": f"error: {type(e).__name__}: {str(e)[:80]}"}


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    # Measured on this library: control (wrong song) scores 0.000 for tiny, base
    # and small alike, so separation is never the constraint -- only margin is.
    # base costs 4.3s/track vs small's 9.6s and keeps ample headroom.
    ap.add_argument("--model", default="base",
                    choices=["tiny", "base", "small", "medium"])
    ap.add_argument("--excerpt", type=float, default=45.0)
    ap.add_argument("--threads", type=int,
                    default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("-j", "--workers", type=int)
    ap.add_argument("--control", action="store_true",
                    help="also score each transcript against an unrelated "
                         "song's lyrics, to calibrate the threshold")
    args = ap.parse_args()

    rows = json.load(open(REVIEW))
    # Only tracks with a proposal worth checking. Auto-accepted ones are
    # included so we can see what a known-good score looks like.
    cands = [r for r in rows if r.get("proposed_artist") and r.get("proposed_title")]
    cands.sort(key=lambda r: r["confidence"])
    if args.limit:
        cands = cands[: args.limit]
    if not cands:
        sys.exit("nothing to verify - run review.py first")

    lyric_cache = json.load(open(LYRIC_CACHE)) if os.path.exists(LYRIC_CACHE) else {}
    done = json.load(open(OUT)) if os.path.exists(OUT) else {}
    session = requests.Session()

    # Real decoded length per file. LRCLIB holds several entries per song and
    # this picks the edit we actually have, not one timed against another master.
    durations = {}
    if os.path.exists(ANALYSIS):
        for v in json.load(open(ANALYSIS)).values():
            if v.get("path") and v.get("decoded_secs"):
                durations[v["path"]] = float(v["decoded_secs"])

    # Read the sheet we hold now, from the cache rather than the network, so
    # deciding what to re-score costs nothing.
    def held(r):
        e = lyric_cache.get(f'{r["proposed_artist"]}|{r["proposed_title"]}'.lower())
        if isinstance(e, dict):
            return e.get("plain") or _strip_lrc(e.get("synced"))
        return e if isinstance(e, str) else None

    def redo(r):
        return (r["path"] not in done or args.control
                or _stale(done[r["path"]], held(r)))

    todo = [r for r in cands if redo(r)]
    results = [done[r["path"]] for r in cands if not redo(r)]
    changed = sum(1 for r in cands
                  if r["path"] in done and not args.control
                  and _stale(done[r["path"]], held(r)))
    print(f"  {len(todo)} to verify ({len(results)} cached, "
          f"{changed} of them re-scored because the sheet changed)")

    # Phase 1: reference lyrics. Network-bound and globally rate-limited inside
    # lyrics_fetch, so threads only hide latency, they do not raise the rate.
    t0 = time.time()
    need, texts = [], {}

    # The Deezer track id makes the lyric lookup exact rather than a search.
    # Read once for the whole sweep, not per track.
    dz = lyrics_fetch.deezer_ids()

    def fetch(r):
        ly = lyrics_fetch.fetch(r["proposed_artist"], r["proposed_title"],
                                lyric_cache, session,
                                album=r.get("proposed_album"),
                                duration=durations.get(r["path"]),
                                deezer_id=dz.get(r["path"]))
        return r, (ly.get("plain") or _strip_lrc(ly.get("synced")))

    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, (r, lyrics) in enumerate(ex.map(fetch, todo), 1):
            rec = {"file": r["file"], "path": r["path"],
                   "confidence": r["confidence"], "tier": r["tier"],
                   "proposed": f'{r["proposed_artist"]} - {r["proposed_title"]}',
                   "have_lyrics": bool(lyrics),
                   # What was actually scored, recorded at the moment it is
                   # scored rather than looked up again later.
                   "lyric_digest": _digest(lyrics)}
            if lyrics:
                texts[r["path"]] = lyrics
                need.append(rec)
            else:
                # Nothing to compare against, so there is no point decoding it.
                rec["verdict"] = "no_reference_lyrics"
                results.append(rec)
                done[r["path"]] = rec
            if i % 100 == 0 or i == len(todo):
                print(f"    lyrics {i}/{len(todo)}  {time.time()-t0:.0f}s", flush=True)
    json.dump(lyric_cache, open(LYRIC_CACHE, "w"), ensure_ascii=False)
    print(f"  {len(need)} have reference lyrics and will be transcribed")

    # Phase 2: transcription. CPU-bound and independent per file -> processes.
    if need:
        workers = args.workers or _worker_default()
        per = max(1, (os.cpu_count() or 4) // workers)
        print(f"  transcribing with {workers} workers x {per} threads", flush=True)
        tasks = [(rec["path"], texts[rec["path"]], args.excerpt,
                  args.model, per, MODELS) for rec in need]
        bypath = {rec["path"]: rec for rec in need}
        t1 = time.time()
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(transcribe_one, t): t[0] for t in tasks}
            for i, f in enumerate(as_completed(futs), 1):
                path, part = f.result()
                rec = bypath[path]
                rec.update(part)
                results.append(rec)
                done[path] = rec
                if i % 25 == 0 or i == len(tasks):
                    el = time.time() - t1
                    print(f"    transcribe {i}/{len(tasks)}  {el:.0f}s  "
                          f"{el/i:.2f}s/track  eta {(len(tasks)-i)*el/i/60:.0f}min",
                          flush=True)
                    json.dump(done, open(OUT, "w"), ensure_ascii=False, indent=1)

    json.dump(done, open(OUT, "w"), ensure_ascii=False, indent=1)
    json.dump(lyric_cache, open(LYRIC_CACHE, "w"), ensure_ascii=False)

    scored = [r for r in results if r.get("score") is not None]
    print(f"\n  verified {len(scored)} of {len(results)} "
          f"({sum(1 for r in results if not r['have_lyrics'])} had no reference lyrics)")
    if scored:
        scored.sort(key=lambda r: r["score"])
        print("\n  score  ctrl   conf  file -> proposed")
        for r in scored:
            c = f'{r.get("control_score"):.2f}' if r.get("control_score") is not None else "  - "
            print(f"  {r['score']:.2f}   {c}  {r['confidence']:.2f}  "
                  f"{r['file'][:36]:36} -> {r['proposed'][:34]}")
        import statistics as st
        print(f"\n  median match score: {st.median(r['score'] for r in scored):.3f}")
        ctrl = [r["control_score"] for r in scored if r.get("control_score") is not None]
        if ctrl:
            print(f"  median control score (wrong song): {st.median(ctrl):.3f}")
            print("  -> the gap between these two is the usable signal")
    print()


if __name__ == "__main__":
    main()
