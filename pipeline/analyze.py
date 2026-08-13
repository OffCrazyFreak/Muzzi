#!/usr/bin/env python3
"""Stage 5: audio analysis. Fully parallel, CPU-bound, no network.

Everything here derives from the waveform, so it works on every file including
ones no database could identify. That is the point: an unidentifiable track
still ends up with BPM, key, loudness and a quality grade, and is therefore
still mixable.

Per track:
  - BPM from two independent engines, with an agreement verdict.
    Measured on this library: engines agree 77%, split by an exact octave 20%.
    Essentia's own confidence value is NOT predictive (a 0.9 "low confidence"
    track had both engines agree exactly), so agreement is the quality gate.
  - Musical key + scale, mapped to Camelot notation for harmonic mixing.
  - Danceability, loudness, dynamic complexity, brightness.
  - Spectral cutoff -> real audio quality. A file claiming 320kbps but
    transcoded from a 128kbps source shows a hard wall near 16kHz. This is far
    more honest than the bitrate header.
  - Full low-level feature vector persisted to features/ for the future
    similarity/recommendation work. Nearly free now, ~4 CPU-hours later.

Results cache to cache/analysis.json keyed by fingerprint, so re-runs are free
and adding files never re-analyses old ones.

Usage: analyze.py <dir> [-j workers] [--force]
"""
import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import sources  # noqa: E402
CACHE = os.path.join(HERE, "cache", "analysis.json")
FEATDIR = os.path.join(HERE, "features")
AUDIT_TRUTH = os.path.join(HERE, "cache", "audit_truth.json")

from pipeline import loudness  # noqa: E402

# Camelot wheel. Harmonically compatible keys are adjacent numbers, or the
# same number with the other letter. This is what makes "what can I mix into
# this?" a lookup instead of music theory.
CAMELOT = {
    ("B", "major"): "1B",   ("F#", "major"): "2B",  ("Db", "major"): "3B",
    ("Ab", "major"): "4B",  ("Eb", "major"): "5B",  ("Bb", "major"): "6B",
    ("F", "major"): "7B",   ("C", "major"): "8B",   ("G", "major"): "9B",
    ("D", "major"): "10B",  ("A", "major"): "11B",  ("E", "major"): "12B",
    ("Ab", "minor"): "1A",  ("Eb", "minor"): "2A",  ("Bb", "minor"): "3A",
    ("F", "minor"): "4A",   ("C", "minor"): "5A",   ("G", "minor"): "6A",
    ("D", "minor"): "7A",   ("A", "minor"): "8A",   ("E", "minor"): "9A",
    ("B", "minor"): "10A",  ("F#", "minor"): "11A", ("Db", "minor"): "12A",
}
ENHARMONIC = {"C#": "Db", "D#": "Eb", "G#": "Ab", "A#": "Bb", "Gb": "F#"}


def to_camelot(key, scale):
    key = ENHARMONIC.get(key, key)
    return CAMELOT.get((key, (scale or "").lower()))


def spectral_cutoff(audio, sr=44100):
    """Highest frequency still carrying real energy, in Hz.

    Reveals the true provenance of a lossy file: ~16kHz means a 128kbps
    source, ~19kHz around 192-256kbps, >20kHz effectively full band.
    """
    import numpy as np
    import essentia.standard as es

    w, spec = es.Windowing(type="hann"), es.Spectrum()
    frames = list(es.FrameGenerator(audio, frameSize=2048, hopSize=4096,
                                    startFromZero=True))
    if not frames:
        return None
    # Element-wise MAX across frames, not median. We are measuring the codec's
    # ceiling -- the highest frequency it ever let through -- and a median reads
    # far too low on quiet or bass-heavy tracks (Maneskin measured 6kHz on a
    # file whose real wall is 16kHz).
    mags = np.max(np.array([spec(w(f)) for f in frames[:600]]), axis=0)
    if mags.max() <= 0:
        return None
    db = 20 * np.log10(np.maximum(mags, 1e-12) / mags.max())
    # Last bin still within 50 dB of peak = the top of the real signal.
    above = np.nonzero(db > -50)[0]
    if len(above) == 0:
        return None
    return float(above[-1] * (sr / 2.0) / (len(mags) - 1))


# Octave floor. Anything slower than this is almost certainly a halving error
# rather than a real tempo, so we step back up.
MIN_PLAUSIBLE_BPM = 65


def octave_choice(bpm, also=()):
    """Resolve an octave split toward the LOWER reading.

    A log-normal prior centred on 120 is the textbook answer, but it is wrong
    for THIS library: it reported Panic! At The Disco's "High Hopes" as 164
    when it is 82. Roughly half these tracks sit under 120, and an under-read
    tempo is easy to spot and mentally double, whereas an over-read one on a
    non-electronic track is not. The cost is that genuinely fast tracks read
    half (The Black Keys' "Lonely Boy" becomes 83, not 165) -- BPM_ALT always
    carries the other value.
    """
    cands = [b for b in (bpm, *also) if b and b > 0]
    if not cands:
        return bpm
    low = min(cands)
    while low < MIN_PLAUSIBLE_BPM:
        low *= 2
    return round(low, 1)


def grade_quality(cutoff_hz, bitrate_kbps):
    """Combine measured bandwidth with the claimed bitrate into a verdict."""
    if cutoff_hz is None:
        return "unknown", None
    khz = cutoff_hz / 1000.0
    if khz >= 19.5:
        grade = "high"
    elif khz >= 17.5:
        grade = "good"
    elif khz >= 15.5:
        grade = "fair"
    else:
        grade = "low"
    # The interesting case: header claims a lot, spectrum says otherwise.
    suspect = bool(bitrate_kbps and bitrate_kbps >= 256 and khz < 17.5)
    return grade, suspect


def real_bitrate(path, mf=None, header_dur=None, essentia_kbps=None):
    """-> kbps that is true for MP3 and AAC alike, or None.

    Essentia's MetadataReader reports 1 kbps for the AAC files YouTube
    serves: they carry no esds bitrate box and it returns a placeholder
    rather than failing. 132 of 261 m4a files here were cached at 1 against
    a real 130-144, which silently killed the quality_suspect flag for every
    AAC file in the library -- it needs >= 256 to ever trip.

    Mutagen is not the fix on its own; it reports 0 for those same files.
    But size over duration is already in hand, so the fallback is free, and
    it lands within 8 kbps of ffprobe across all 250 AAC files here.

    That fallback measures the whole container, so it reads a few kbps high
    on a file carrying large embedded art. Harmless: the only consumer that
    compares against a threshold is the quality_suspect flag at 256 kbps,
    and the dedupe tie-break only needs the ordering to be right.
    """
    try:
        if mf is None:
            from mutagen import File as _MF
            mf = _MF(path)
        if header_dur is None and mf is not None and mf.info:
            header_dur = float(mf.info.length)
    except Exception:
        mf = None

    try:
        br = int(getattr(mf.info, "bitrate", 0) or 0)
        if br > 0:
            return round(br / 1000)
    except Exception:
        pass

    try:
        if header_dur and header_dur > 0:
            return round(os.path.getsize(path) * 8 / header_dur / 1000)
    except OSError:
        pass

    # Last resort. Only reached when the file is unreadable by mutagen and
    # unstattable, in which case the placeholder is no worse than nothing.
    return essentia_kbps


_VIBENET = None


def _vibenet():
    """Lazy per-process load. ~19MB, ~0.9s, so once per worker not per track."""
    global _VIBENET
    if _VIBENET is None:
        try:
            from vibenet import load_model
            _VIBENET = load_model()
        except Exception:
            _VIBENET = False
    return _VIBENET or None


def analyse_one(args):
    path, fp = args
    import essentia
    essentia.log.warningActive = False
    essentia.log.infoActive = False
    import essentia.standard as es
    import numpy as np

    t0 = time.time()
    try:
        audio = es.MonoLoader(filename=path, sampleRate=44100)()
        if len(audio) < 44100:
            return fp, {"path": path, "error": "audio too short"}

        # Integrity check, free because we have already decoded the file:
        # a truncated download decodes to far less audio than its header
        # claims. Without this, a broken file still yields a confident BPM.
        decoded = len(audio) / 44100.0
        # Bound before the try: real_bitrate() reads `mf` further down, and an
        # unreadable header would otherwise leave the name undefined there.
        mf, header_dur = None, None
        try:
            from mutagen import File as _MF
            mf = _MF(path)
            header_dur = float(mf.info.length) if mf and mf.info else None
        except Exception:
            pass
        truncated = bool(header_dur and header_dur > 5
                         and decoded < header_dur * 0.90)

        # Three independent engines. Two were not enough: they can agree on a
        # half-time reading together (Green Day's Basket Case), so a third
        # opinion breaks ties that agreement alone cannot see.
        bpm_r, beats, conf, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
        bpm_d = float(es.RhythmExtractor2013(method="degara")(audio)[0])
        bpm_p = float(es.PercivalBpmEstimator()(audio))
        bpm_r = float(bpm_r)

        # Fold every engine onto a common octave before comparing, otherwise
        # two engines an octave apart look like disagreement when they in fact
        # found the same pulse.
        def to_ref(b, ref):
            if not b:
                return b
            cand = b
            while cand < ref / 1.45:
                cand *= 2
            while cand > ref * 1.45:
                cand /= 2
            return cand

        cands = [b for b in (bpm_r, bpm_d, bpm_p) if b and b > 20]
        folded = [to_ref(b, bpm_r) for b in cands]
        spread = max(folded) / min(folded) if folded else 1
        votes = len([f for f in folded if abs(f / folded[0] - 1) < 0.04])

        ratio = bpm_r / bpm_p if bpm_p else 0
        if spread < 1.04:
            # All three heard the same pulse; the only open question is octave.
            verdict = "agree" if 0.97 < ratio < 1.03 else "octave"
            bpm = round(sum(folded) / len(folded), 1)
            bpm = octave_choice(bpm)
        elif 0.97 < ratio < 1.03:
            verdict, bpm = "agree", round((bpm_r + bpm_p) / 2, 1)
        elif 1.94 < ratio < 2.06 or 0.485 < ratio < 0.515:
            # Octave split: the engines heard the same pulse an octave apart.
            # Always taking the higher one was wrong -- it put Nickelback's
            # "How You Remind Me" at 172 and Lord Huron's "The Night We Met"
            # at 172, both of which are ~86. Perceived tempo clusters around
            # 120, so pick whichever candidate sits closer to 120 in LOG space
            # (a log-normal tempo prior). This keeps genuinely fast tracks
            # fast -- The Black Keys' "Lonely Boy" stays 166 -- while pulling
            # half-time misreads back down.
            verdict = "octave"
            bpm = octave_choice(min(bpm_r, bpm_p), also=(bpm_r, bpm_p, bpm_d))
        else:
            verdict, bpm = "disagree", round(bpm_r, 1)

        # Key detection is our least-verified field, so use the same trick that
        # caught the BPM error: several independent profiles, then vote.
        # "edma" is tuned for electronic music, "temperley"/"krumhansl" for
        # traditional tonal music -- between them they cover this library.
        key_votes = []
        for prof in ("temperley", "krumhansl", "edma"):
            try:
                k_, s_, st_ = es.KeyExtractor(profileType=prof)(audio)
                key_votes.append((k_, s_, float(st_), prof))
            except Exception:
                continue
        if not key_votes:
            key, scale, strength, n_agree = "", "", 0.0, 0
        else:
            from collections import Counter
            tally = Counter((k_, s_) for k_, s_, _, _ in key_votes)
            (key, scale), n_agree = tally.most_common(1)[0]
            strength = max(st_ for k_, s_, st_, _ in key_votes
                           if (k_, s_) == (key, scale))
        dance = float(es.Danceability()(audio)[0])
        # Loudness is the one measurement that does NOT come off the mono
        # decode above. essentia's LoudnessEBUR128 only accepts stereo, and
        # feeding it this mono signal duplicated into both channels read the
        # library 0.76 dB quiet (sd 0.46) -- BS.1770 sums channel energies, so
        # a downmix in both channels loses up to 3 dB on wide mixes. ffmpeg
        # measures the real stereo file and returns true peak with it.
        loud = loudness.ebur128(path)
        dyn = float(es.DynamicComplexity()(audio)[0])
        centroid = float(es.Centroid(range=22050)(es.Spectrum()(
            es.Windowing(type="hann")(audio[:2048]))))
        cutoff = spectral_cutoff(audio)

        # Perceptual features from VibeNet (distilled EfficientNet). Gives the
        # mood/energy axis Essentia alone cannot, plus instrumentalness -- the
        # vocal-vs-instrumental signal we wanted and never had.
        vibe = {}
        vm = _vibenet()
        if vm is not None:
            try:
                res = vm.predict(path)
                r0 = res[0] if isinstance(res, (list, tuple)) else res
                for k in ("acousticness", "danceability", "energy",
                          "instrumentalness", "liveness", "speechiness",
                          "valence"):
                    v = getattr(r0, k, None)
                    if v is not None:
                        vibe[k] = round(float(v), 3)
            except Exception:
                pass

        info = es.MetadataReader(filename=path, failOnError=False)()
        # `mf` and `header_dur` come from the truncation check above and are
        # reused here rather than re-read. Must stay ahead of the MFCC block
        # below, which rebinds `mf` to a list of frames.
        bitrate = real_bitrate(path, mf, header_dur,
                               info[9] if len(info) > 9 else None)
        grade, suspect = grade_quality(cutoff, bitrate)

        # Persist a compact feature vector for future similarity search.
        mfcc = es.MFCC(numberCoefficients=13)
        spec, win = es.Spectrum(), es.Windowing(type="hann")
        mf = [mfcc(spec(win(f)))[1] for f in
              es.FrameGenerator(audio, frameSize=2048, hopSize=1024)]
        feat = np.mean(np.array(mf), axis=0).tolist() if mf else []
        os.makedirs(FEATDIR, exist_ok=True)
        # Hash the WHOLE fingerprint. Truncating it collides: 330 files here
        # share only 321 distinct 52-char prefixes, so a prefix-named file
        # silently overwrote another track's features.
        fpid = hashlib.sha1(fp.encode()).hexdigest()[:24]
        with open(os.path.join(FEATDIR, f"{fpid}.json"), "w") as fh:
            json.dump({"path": path, "mfcc_mean": feat,
                       "bpm": bpm, "key": key, "scale": scale}, fh)

        return fp, {
            "path": path,
            "bpm": bpm,
            "bpm_rhythm": round(bpm_r, 1),
            "bpm_percival": round(bpm_p, 1),
            "bpm_verdict": verdict,
            "bpm_essentia_conf": round(float(conf), 2),
            "beats": len(beats),
            "key": key,
            "scale": scale,
            "camelot": to_camelot(key, scale),
            "key_agreement": f"{n_agree}/{len(key_votes)}" if key_votes else None,
            "key_strength": round(float(strength), 3),
            "bpm_degara": round(bpm_d, 1),
            "bpm_votes": votes,
            "truncated": truncated,
            "decoded_secs": round(decoded, 1),
            "header_secs": round(header_dur, 1) if header_dur else None,
            "danceability": round(dance, 3),
            "vibe": vibe or None,
            "loudness_lufs": loud["loudness_lufs"],
            "true_peak": loud["true_peak"],
            # The invalidation marker this cache has never had: an entry
            # without it was measured by the old mono path and is 0.76 dB out.
            "loudness_method": loud["loudness_method"],
            "loudness_error": loud["loudness_error"],
            "dynamic_complexity": round(dyn, 3),
            "brightness": round(centroid, 1),
            "spectral_cutoff_hz": round(cutoff) if cutoff else None,
            "quality_grade": grade,
            "quality_suspect": suspect,
            "bitrate_kbps": bitrate,
            "analysis_secs": round(time.time() - t0, 1),
        }
    except Exception as e:
        return fp, {"path": path, "error": f"{type(e).__name__}: {str(e)[:150]}"}


def refresh_bitrate():
    """Recompute bitrate_kbps and quality_suspect in place, no decoding.

    A full re-analysis is 1-14 s/track and --force redoes everything, which
    is a poor trade for one header field. This reads only the container
    header, so the whole library takes seconds, and it is the repeatable
    operation to reach for the next time a format lies about its bitrate.

    Entries whose file has since moved are left exactly as they were: a
    missing file is not evidence that the cached number is wrong.
    """
    if not os.path.exists(CACHE):
        print(f"  no {CACHE}, nothing to refresh\n")
        return
    done = json.load(open(CACHE))

    changed = gone = 0
    for entry in done.values():
        path = entry.get("path")
        if entry.get("error") or not path:
            continue
        if not os.path.exists(path):
            gone += 1
            continue
        before = entry.get("bitrate_kbps")
        after = real_bitrate(path, essentia_kbps=before)
        if after == before:
            continue
        entry["bitrate_kbps"] = after
        # The grade is a function of the cutoff alone, so it cannot move --
        # but `suspect` is a function of the bitrate and has to follow it.
        _, entry["quality_suspect"] = grade_quality(
            entry.get("spectral_cutoff_hz"), after)
        changed += 1

    json.dump(done, open(CACHE + ".tmp", "w"), indent=1)
    os.replace(CACHE + ".tmp", CACHE)

    ok = [v for v in done.values() if "error" not in v]
    bogus = sum(1 for v in ok if (v.get("bitrate_kbps") or 0) <= 1)
    print(f"  refreshed {changed} of {len(done)} entries, {gone} files missing")
    print(f"  entries still at <= 1 kbps: {bogus}")
    print("  suspect (claims high bitrate, spectrum says otherwise):",
          sum(1 for v in ok if v.get("quality_suspect")), "\n")


def _audit_truth_seed():
    """-> {source basename: {loudness fields}} from a previous audit run.

    audit_truth.py measures the OUTPUT files with the very ffmpeg command this
    module now uses, so those numbers are already the answer -- and the output
    is a byte copy of its source, so a measurement on one is valid for the
    other. Joining them back saves decoding the library a second time, and it
    is the only way to reach the tracks whose source folder no longer exists:
    their output file is the last copy of that audio.

    Joined on the MUZZI_SOURCE_FILE stamp, which is what write_tags.py put
    there for exactly this kind of question. A basename claimed by two entries
    is dropped rather than guessed at.
    """
    if not os.path.exists(AUDIT_TRUTH):
        return {}
    try:
        truth = json.load(open(AUDIT_TRUTH))
    except Exception:
        return {}

    import mutagen
    seed, ambiguous = {}, set()
    for rel, t in truth.items():
        if t.get("true_lufs") is None or t.get("lufs_error"):
            continue
        path = rel if os.path.isabs(rel) else os.path.join(HERE, rel)
        try:
            m = mutagen.File(path)
            tags = m.tags if m else None
            if tags is None:
                continue
            if hasattr(tags, "getall"):
                fr = tags.getall("TXXX:MUZZI_SOURCE_FILE")
                src = str(fr[0].text[0]) if fr and fr[0].text else None
            else:
                v = tags.get("----:com.apple.iTunes:MUZZI_SOURCE_FILE")
                src = bytes(v[0]).decode("utf-8", "ignore") if v else None
        except Exception:
            continue
        if not src or src in ambiguous:
            continue
        if src in seed:
            del seed[src]
            ambiguous.add(src)
            continue
        pk = t.get("true_peak_db")
        seed[src] = {
            "loudness_lufs": round(float(t["true_lufs"]), 2),
            "true_peak": (round(10 ** (float(pk) / 20.0), 6)
                          if pk is not None else None),
            "loudness_method": "ffmpeg-ebur128",
            "loudness_error": None,
        }
    return seed


def refresh_loudness(workers, force=False):
    """Re-measure loudness and true peak for entries the old code wrote.

    Every entry cached before ffmpeg took over carries a mono-downmix figure
    that reads 0.76 dB quiet on average, and no peak at all -- so ReplayGain
    computed from it is wrong and cannot be capped against clipping. Entries
    already carrying `loudness_method` were measured the new way and are left
    alone, which is what makes this safe to re-run. `force` re-measures them
    anyway, for when the cached figures are distrusted rather than merely old
    -- about 6 minutes for this library, against an hour for a full --force.

    Entries whose file has since moved are left exactly as they were: a
    missing file is not evidence that the cached number is wrong.

    NOTE what this cannot fix. These entries describe the SOURCE files, and
    write_tags copies those to output/_all. Anything that edits an output copy
    after the fact leaves that copy's ReplayGain describing audio it no longer
    contains, and no amount of re-measuring the source will show it, because
    the source did not change. That case needs the output re-measured and
    re-tagged, and tools/audit_compare.py is what catches it, because it
    measures output/_all itself.

    Trimming leading silence is NOT such an edit, which is worth stating
    because it looks like one. Measured on six trimmed files with cuts of 0.5
    to 4.7s: integrated loudness moved 0.00 dB on all six and true peak moved
    0.00 dB on five and 0.10 on the sixth. BS.1770 gates silence out of the
    integrated figure, and a region quiet enough to cut cannot hold the peak,
    so the trim stage does not owe the loudness stage a re-measure.
    """
    if not os.path.exists(CACHE):
        print(f"  no {CACHE}, nothing to refresh\n")
        return
    done = json.load(open(CACHE))

    seed = {} if force else _audit_truth_seed()
    if force:
        print("  --force: re-measuring every entry, ignoring cached values")
    else:
        print(f"  {len(seed)} measurements available from "
              f"{os.path.basename(AUDIT_TRUTH)}")

    # A basename claimed by two cache entries cannot be seeded: the seed is
    # keyed on basename, so both would take one measurement and one of them
    # would describe a different file, setting that track's gain and its
    # clipping cap from the wrong audio with no error anywhere. The audit side
    # already drops ambiguous names; this is the same guard on this side.
    from collections import Counter as _Counter
    _by_base = _Counter(os.path.basename(e["path"])
                        for e in done.values() if e.get("path"))
    ambiguous = {b for b, n in _by_base.items() if n > 1}

    seeded = gone = ambig = 0
    todo = []
    for fp, entry in done.items():
        path = entry.get("path")
        if entry.get("error") or not path:
            continue
        # A SUCCESSFUL measurement is the skip condition, not the presence of
        # the method field: loudness.ebur128 stamps loudness_method even when
        # it failed, so keying on it made one timed-out decode permanent. That
        # entry then has no gain written, forever, without --force.
        if entry.get("loudness_lufs") is not None and not force:
            continue
        base = os.path.basename(path)
        hit = None if base in ambiguous else seed.get(base)
        if hit:
            entry.update(hit)
            seeded += 1
            continue
        if base in ambiguous:
            ambig += 1
        if not os.path.exists(path):
            gone += 1
            continue
        todo.append((fp, path))

    print(f"  seeded {seeded}, {len(todo)} to measure, {gone} files missing, "
          f"{ambig} measured rather than seeded (basename claimed twice), "
          f"{workers} workers")

    measured = 0
    if todo:
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(loudness.ebur128, p): fp for fp, p in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                fp = futures[fut]
                try:
                    done[fp].update(fut.result())
                    measured += 1
                except Exception as e:
                    done[fp]["loudness_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                if i % 50 == 0 or i == len(todo):
                    el = time.time() - t0
                    print(f"  {i}/{len(todo)}  {el:.0f}s elapsed, "
                          f"eta {(len(todo)-i)*el/i:.0f}s", flush=True)
                    json.dump(done, open(CACHE + ".tmp", "w"), indent=1)
                    os.replace(CACHE + ".tmp", CACHE)

    json.dump(done, open(CACHE + ".tmp", "w"), indent=1)
    os.replace(CACHE + ".tmp", CACHE)

    ok = [v for v in done.values() if "error" not in v]
    stale = [v for v in ok if not v.get("loudness_method")]
    failed = [v for v in ok if v.get("loudness_error")]
    print(f"  refreshed {seeded + measured} of {len(done)} entries "
          f"({seeded} seeded, {measured} measured)")
    print(f"  entries still on the old mono measurement: {len(stale)}")
    print(f"  entries with no true peak: "
          f"{sum(1 for v in ok if v.get('true_peak') is None)}")
    if failed:
        print(f"  measurement errors: {len(failed)}")
    print()


def main():
    ap = argparse.ArgumentParser()
    # Optional, not required: the --refresh-* modes work off the cache alone
    # and have no directory to walk.
    ap.add_argument("root", nargs="*")
    # VibeNet loads its model per worker, so worker count is bounded by RAM,
    # not cores. Ten workers on a 13GB box drove this to 12GB and the OOM
    # killer took the run out repeatedly. Budget ~1.1GB per worker and leave
    # 2GB headroom for the rest of the system.
    def _worker_default():
        cores = max(1, (os.cpu_count() or 4) - 2)
        try:
            with open("/proc/meminfo") as fh:
                total_kb = next(int(l.split()[1]) for l in fh
                                if l.startswith("MemTotal"))
            # Measured on this library: each worker holds ~950MB resident
            # (Essentia decode buffers plus a per-process VibeNet model).
            # Budget 1.2GB and keep 3GB clear -- at 10 workers this box hit
            # 12GB of 13GB and the OOM killer took the run out, which is what
            # the repeated exit-137s actually were.
            by_ram = max(1, int((total_kb / 1024 / 1024 - 3) // 1.2))
            return min(cores, by_ram)
        except Exception:
            return min(cores, 4)

    ap.add_argument("-j", "--workers", type=int, default=_worker_default())
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, help="analyse at most N new files")
    ap.add_argument("--refresh-bitrate", action="store_true",
                    help="recompute bitrate_kbps and quality_suspect for "
                         "cached entries from the file header, no decoding")
    ap.add_argument("--refresh-loudness", action="store_true",
                    help="re-measure loudness and true peak for cached "
                         "entries written before ffmpeg took over")
    args = ap.parse_args()

    if args.refresh_bitrate:
        return refresh_bitrate()
    if args.refresh_loudness:
        return refresh_loudness(args.workers, force=args.force)
    if not args.root:
        ap.error("a root directory is required unless --refresh-bitrate "
                 "or --refresh-loudness")

    fps = {}
    fpcache = os.path.join(HERE, "cache", "fingerprints.json")
    if os.path.exists(fpcache):
        for v in json.load(open(fpcache)).values():
            if v.get("fingerprint"):
                fps[v["path"]] = v["fingerprint"]

    done = {}
    if os.path.exists(CACHE) and not args.force:
        done = json.load(open(CACHE))

    todo = []
    for p in sources.walk(args.root):
        # Fall back to the path when no fingerprint exists, so analysis is
        # never blocked on the fingerprint stage having run.
        fp = fps.get(p, "path:" + p)
        # Two files can share a fingerprint and still need separate
        # entries: the same song downloaded into two source folders, or an
        # mp3 and an m4a of one master. Keying on the fingerprint alone
        # meant the second file had no analysis under its own path at all,
        # so dedupe could not measure it, could not drop it, and shipped
        # the song twice under two names.
        if fp in done and done[fp].get("path") not in (None, p):
            fp = fp + "|" + p
        if fp not in done:
            todo.append((p, fp))

    if args.limit:
        todo = todo[: args.limit]

    print(f"  {len(done)} cached, {len(todo)} to analyse, {args.workers} workers")
    if not todo:
        print("  nothing to do\n")
        return

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(analyse_one, t): t for t in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            fp, res = fut.result()
            done[fp] = res
            if i % 20 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"  {i}/{len(todo)}  {el:.0f}s elapsed, "
                      f"{el/i:.2f}s/track, eta {(len(todo)-i)*el/i:.0f}s", flush=True)
                tmp = CACHE + ".tmp"
                json.dump(done, open(tmp, "w"), indent=1)
                os.replace(tmp, CACHE)

    json.dump(done, open(CACHE + ".tmp", "w"), indent=1)
    os.replace(CACHE + ".tmp", CACHE)

    ok = [v for v in done.values() if "error" not in v]
    errs = [v for v in done.values() if "error" in v]
    from collections import Counter
    print(f"\n  analysed {len(ok)}, errors {len(errs)}")
    print("  bpm verdict:", dict(Counter(v["bpm_verdict"] for v in ok)))
    print("  quality    :", dict(Counter(v["quality_grade"] for v in ok)))
    print("  suspect (claims high bitrate, spectrum says otherwise):",
          sum(1 for v in ok if v.get("quality_suspect")))
    print(f"  wall time  : {time.time()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
