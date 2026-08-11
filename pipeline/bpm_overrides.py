#!/usr/bin/env python3
"""Hand-checked BPM corrections, applied in one place.

The three engines agree on a value fairly often and are still an octave out:
a slow rap track whose hi-hats run at double the felt tempo reads 160+ on all
three, so prefer-lower in analyze.py has nothing to choose between. Those cases
can only be settled per track, by ear or against a reference, which is what
config/bpm_overrides.json records.

Actions:
  halve          -- publish the half value; the fast reading was double-time
  set            -- publish a reference value outright, when an external
                    source disagrees with us by something other than an octave
  keep           -- the fast reading is real (rock, ska, eurodance)
  leave          -- undecided; no reference found, left as measured
  flag_identity  -- the tempo contradicts the reference for the proposed
                    match, so the MATCH is suspect, not the tempo

Only 'halve' and 'set' change a number. The rest are recorded so a later run
does not re-litigate them and so review.py can surface them.

'set' exists because Deezer publishes a tempo for 653 of these tracks, and
where it disagrees with us by a non-octave ratio it has been right every time
it could be checked by hand -- Disturbed's "The Sound of Silence", Eminem's
"Cleanin' Out My Closet", "Hey Mama". Octave disagreements are deliberately
NOT included: which of 85 and 170 to publish is a judgement about how the song
feels, and prefer-lower settles those.
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(HERE, "config", "bpm_overrides.json")


def load():
    if not os.path.exists(PATH):
        return {}
    try:
        with open(PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def apply(audio, src_path, table):
    """Return (audio, note). `audio` is copied only when something changes."""
    if not table:
        return audio, None
    o = table.get(os.path.basename(src_path))
    if not o:
        return audio, None
    if o["action"] == "halve" and audio.get("bpm"):
        a = dict(audio)
        a["bpm"] = o["to"]
        a["bpm_override"] = "halved"
        a["bpm_override_reason"] = o["reason"]
        a["bpm_override_verified"] = bool(o.get("verified"))
        # The old value is worth keeping: if a correction turns out to be
        # wrong, the measurement is still in the file.
        a["bpm_measured"] = o["from"]
        return a, "halved"
    if o["action"] == "set" and audio.get("bpm"):
        a = dict(audio)
        a["bpm"] = o["to"]
        a["bpm_override"] = "reference"
        a["bpm_override_reason"] = o["reason"]
        a["bpm_override_source"] = o.get("source")
        a["bpm_measured"] = o["from"]
        return a, "reference"
    if o["action"] in ("keep", "leave", "flag_identity"):
        a = dict(audio)
        a["bpm_override"] = o["action"]
        a["bpm_override_reason"] = o["reason"]
        return a, o["action"]
    return audio, None
