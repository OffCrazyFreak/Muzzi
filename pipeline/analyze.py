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
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(HERE, "cache", "analysis.json")
FEATDIR = os.path.join(HERE, "features")

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
        header_dur = None
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
        loud = float(es.LoudnessEBUR128()(np.array([audio, audio]).T)[2])
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
        bitrate = info[9] if len(info) > 9 else None
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
            "loudness_lufs": round(loud, 2),
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="+")
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
    args = ap.parse_args()

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
    for root in args.root:
     for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if not n.lower().endswith((".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav")):
                continue
            p = os.path.join(dirpath, n)
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
