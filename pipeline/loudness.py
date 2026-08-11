#!/usr/bin/env python3
"""Integrated loudness and true peak, measured by ffmpeg's ebur128 filter.

One implementation, two callers: the analysis stage tags from it, and
audit_truth.py grades the written tags against it. They have to be the same
code or the audit is comparing two measurements rather than checking one --
which is how the AAC bitrate bug survived, a copied function drifting from
the original.

Why ffmpeg rather than essentia, which is already a dependency and already
decodes every file:

  * essentia's LoudnessEBUR128 only accepts a stereo signal, and analyze.py
    has a mono decode. Handing it that mono signal duplicated into both
    channels is what read this library 0.76 dB quiet -- BS.1770 sums channel
    energies, so a downmix in both channels loses up to 3 dB on wide mixes.
  * essentia's TruePeakDetector measures 13.3 s/track here, against 15.5 s
    for the entire rest of the analysis. ffmpeg returns loudness AND true
    peak together in ~1.9 s.
"""
import re
import subprocess

# ffmpeg prints the summary to stderr. With peak=true it emits a "True peak:"
# section whose "Peak:" line is the last one, hence findall()[-1].
_I = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)
_PEAK = re.compile(r"Peak:\s*(-?[\d.]+)\s*dBFS")


def measure(path, pre=""):
    """-> (integrated LUFS, true peak dBFS, error, decode_error_count).

    `pre` prepends filters, which audit_truth.py uses for its mono pass.
    """
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
    # it printed covers the decoded prefix, not the file. Returning it would
    # tag a track with the loudness of however much of it decoded.
    if r.returncode != 0:
        return None, None, f"ffmpeg exit {r.returncode}: {err.strip()[-120:]}", bad
    mi, mp = _I.search(err), _PEAK.findall(err)
    return (float(mi.group(1)) if mi else None,
            float(mp[-1]) if mp else None, None, bad)


def ebur128(path):
    """-> the loudness fields an analysis entry carries.

    `true_peak` is LINEAR, not dB: ReplayGain peak tags are a 0-1 scale where
    1.0 is full scale, and lossy decoding routinely exceeds it.
    """
    lufs, peak_db, err, bad = measure(path)
    return {
        "loudness_lufs": round(lufs, 2) if lufs is not None else None,
        "true_peak": (round(10 ** (peak_db / 20.0), 6)
                      if peak_db is not None else None),
        "loudness_method": "ffmpeg-ebur128",
        "decode_errors": bad,
        "loudness_error": err,
    }
