#!/usr/bin/env python3
"""Measure ground truth from the audio itself, for auditing written tags.

verify.py reads tags only. This measures duration, bitrate and integrated
LUFS with ffprobe and ffmpeg, so tag values can be checked against the file
they claim to describe. Spectral cutoff is measured separately, by
audit_cutoff.py, which needs essentia; the two reports are joined by
audit_compare.py.

Usage: audit_truth.py DIR OUT.json [-j N]
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_I = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)
_PEAK = re.compile(r"Peak:\s*(-?[\d.]+)\s*dBFS")


def probe(path):
    """-> container/stream facts from ffprobe, no decoding."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration,bit_rate,format_name:"
           "stream=codec_name,bit_rate,sample_rate,channels,duration",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           check=False)
        # A non-zero exit means ffprobe rejected the file. Parsing its empty
        # stdout would report a damaged file as merely missing data, which
        # reads as "nothing to see" in the comparison.
        if r.returncode != 0:
            return {"probe_error": f"ffprobe exit {r.returncode}: "
                                   f"{(r.stderr or '').strip()[:120]}"}
        d = json.loads(r.stdout or "{}")
    except Exception as e:
        return {"probe_error": f"{type(e).__name__}: {str(e)[:120]}"}
    fmt = d.get("format") or {}
    st = next((s for s in d.get("streams") or []
               if s.get("codec_name") not in (None, "mjpeg", "png")), {})
    def num(v, cast=float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None
    return {
        "probe_secs": num(fmt.get("duration")),
        "probe_bitrate_kbps": (lambda b: round(b / 1000) if b else None)(
            num(fmt.get("bit_rate"))),
        "stream_bitrate_kbps": (lambda b: round(b / 1000) if b else None)(
            num(st.get("bit_rate"))),
        "codec": st.get("codec_name"),
        "sample_rate": num(st.get("sample_rate"), int),
        "channels": num(st.get("channels"), int),
    }


def _ebur128(path, pre=""):
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", path, "-af",
           pre + "ebur128=peak=true", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                           check=False)
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:120]}", 0
    err = r.stderr or ""
    bad = len(re.findall(r"Header missing|Error while decoding", err))
    # A non-zero exit means ffmpeg stopped early, so any integrated loudness
    # it printed covers the decoded prefix, not the file. This is ground
    # truth other numbers are graded against, and a partial measurement
    # presented as whole-file loudness is worse than no measurement at all:
    # it would be silently compared against the tag as if it were valid.
    if r.returncode != 0:
        return None, None, f"ffmpeg exit {r.returncode}: {err.strip()[-120:]}", bad
    mi, mp = _I.search(err), _PEAK.findall(err)
    return (float(mi.group(1)) if mi else None,
            float(mp[-1]) if mp else None, None, bad)


def loudness(path):
    """-> integrated LUFS, measured two ways.

    `true_lufs` is the canonical EBU R128 figure for the file as it stands --
    what any player or ReplayGain tool computes, and therefore what a
    LOUDNESS_LUFS tag has to agree with.

    `mono_lufs` is the same measurement on a mono downmix. analyze.py fed
    essentia a mono decode duplicated into two channels and essentia returned
    the mono figure, so this column is here to confirm that hypothesis across
    the whole corpus rather than on the one file it was spotted on.
    """
    i, pk, err, bad = _ebur128(path)
    # The mono pass has its own failure mode -- it adds a resample and a
    # channel-layout conversion the stereo pass does not run. Dropping its
    # error would leave mono_lufs empty with nothing saying why, and that
    # column is the whole basis of the mono-downmix comparison.
    mi, _, mono_err, _ = _ebur128(
        path, "aresample=44100,aformat=channel_layouts=mono,")
    out = {"true_lufs": i, "true_peak_db": pk, "mono_lufs": mi,
           "decode_errors": bad}
    if err:
        out["lufs_error"] = err
    if mono_err:
        out["mono_lufs_error"] = mono_err
    return out


def one(path):
    out = {"path": path}
    out.update(probe(path))
    out.update(loudness(path))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("out")
    ap.add_argument("-j", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    a = ap.parse_args()

    files = sorted(os.path.join(dp, f)
                   for dp, _, fs in os.walk(a.root) for f in fs
                   if f.lower().endswith((".mp3", ".flac", ".m4a", ".ogg",
                                          ".opus", ".wav")))
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
