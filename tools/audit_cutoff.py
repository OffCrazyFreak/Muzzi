#!/usr/bin/env python3
"""Re-measure spectral cutoff on the OUTPUT files, using analyze.py's own code.

The QUALITY tag is derived from the cutoff and the bitrate. Recomputing the
cutoff with the identical function on the file that actually carries the tag
is the only way to tell a correct grade from one inherited off a different
file. Deliberately imports spectral_cutoff/grade_quality from analyze rather
than reimplementing them, so this measures the pipeline, not a lookalike.

Usage: audit_cutoff.py DIR OUT.json [-j N]
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

AUDIO_EXT = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav")


def one(path):
    try:
        import essentia.standard as es
        from pipeline.analyze import spectral_cutoff, grade_quality
        audio = es.MonoLoader(filename=path, sampleRate=44100)()
        cut = spectral_cutoff(audio)
        info = es.MetadataReader(filename=path, failOnError=False)()
        br = info[9] if len(info) > 9 else None
        grade, suspect = grade_quality(cut, br)
        return {"path": path,
                "true_cutoff_hz": round(cut) if cut else None,
                "essentia_bitrate_kbps": br,
                "true_quality_grade": grade,
                "true_quality_suspect": suspect,
                "true_decoded_secs": round(len(audio) / 44100.0, 1)}
    except Exception as e:
        return {"path": path, "error": f"{type(e).__name__}: {str(e)[:150]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("out")
    ap.add_argument("-j", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    a = ap.parse_args()
    files = sorted(os.path.join(dp, f)
                   for dp, _, fs in os.walk(a.root) for f in fs
                   if f.lower().endswith(AUDIO_EXT))
    print(f"{len(files)} files, {a.j} workers", flush=True)
    res = {}
    with cf.ProcessPoolExecutor(max_workers=a.j) as ex:
        for i, r in enumerate(ex.map(one, files, chunksize=4), 1):
            res[r["path"]] = r
            if i % 100 == 0:
                print(f"  {i}/{len(files)}", flush=True)
    with open(a.out, "w") as fh:
        json.dump(res, fh)
    print("wrote", a.out, len(res), flush=True)


if __name__ == "__main__":
    main()
