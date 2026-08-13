#!/usr/bin/env python3
"""One tag reader, shared by every tool that inspects the written output.

There is exactly one copy of this on purpose. The MP3 path and the MP4 path
read the same tag out of two entirely different containers, and every time
they have been written twice, one copy has been the one nobody updated: MP4
freeform atoms hold raw bytes, so `str()` on one gives "b'hr'" rather than
"hr", and that has already reached the library once.

Not a pipeline stage. Reads audio, writes nothing.
"""
import hashlib
import os

import mutagen
from mutagen.mp4 import MP4

# ID3 frames whose value is one string. Read through their frame id so a
# freeform TXXX of the same name cannot shadow the real frame.
_ID3_TEXT = {
    "TIT2": "title", "TPE1": "artist", "TPE2": "albumartist",
    "TPE4": "remixer", "TALB": "album", "TDRC": "year", "TCON": "genre",
    "TBPM": "bpm", "TKEY": "key", "TLAN": "language", "TPUB": "publisher",
    "TSRC": "isrc", "TRCK": "track", "TPOS": "disc",
}

# The same fields in MP4's four-character atom scheme. Written by
# write_generic(), which is the half of write_tags.py that has historically
# lagged behind the ID3 half.
_MP4_TEXT = {
    "\xa9nam": "title", "\xa9ART": "artist", "aART": "albumartist",
    "\xa9alb": "album", "\xa9day": "year", "\xa9gen": "genre",
    "tmpo": "bpm", "\xa9wrt": "composer", "\xa9cmt": "comment",
    "\xa9lyr": "lyrics",
}

# Track and disc are pairs of numbers here, not text, so they need their own
# reader. They were missing from the map above, which meant a stale track
# number on an m4a was invisible to every snapshot: the diff read clean while
# the file still carried the position it had on somebody else's album.
_MP4_PAIR = {"trkn": "track", "disk": "disc"}


# Vorbis comment keys, as write_generic writes them, mapped onto the same
# field names the other two containers produce. Anything not listed keeps its
# own upper-case key, exactly like a freeform TXXX or MP4 atom.
_VORBIS_TEXT = {
    "TITLE": "title", "ARTIST": "artist", "ALBUMARTIST": "albumartist",
    "ALBUM": "album", "DATE": "year", "GENRE": "genre", "BPM": "bpm",
    "LYRICS": "lyrics", "ISRC": "isrc", "TRACKNUMBER": "track",
}


def _text(value):
    """-> a comparable string, whatever mutagen handed back.

    Freeform MP4 atoms are bytes; ID3 frames are objects with .text; MP4
    integer atoms are ints, and tmpo arrives as a one-element list.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v) for v in value)
    return str(value)


def _add(out, key, value):
    """Record a freeform tag without ever losing one to another.

    A file can genuinely carry two frames of the same name. Overwriting keeps
    whichever mutagen happened to yield last, which makes the snapshot depend
    on frame order rather than on content; joining shows both and makes the
    duplication itself visible in the diff.
    """
    if key in out and out[key] != value:
        out[key] = f"{out[key]} | {value}"
    else:
        out[key] = value


def _hashed(text):
    """-> a stable marker for a body too long to print in a diff.

    Lyrics are thousands of characters. A diff that prints both copies is a
    diff nobody reads, and the question being asked is only ever "did this
    change", so the answer is a hash and a length.
    """
    if text is None:
        return None
    b = text.encode("utf-8", "ignore")
    return f"sha256:{hashlib.sha256(b).hexdigest()[:16]} chars={len(text)}"


def tags_of(path, hash_long=True):
    """-> flat dict of everything tagged on this file, MP3 and MP4 alike.

    Keys are lower case field names for the standard fields, upper case for
    the freeform ones (TXXX on ID3, ----:com.apple.iTunes: on MP4), so a
    MUZZI_* stamp reads the same whichever container it came out of.

    hash_long=False returns lyrics and comment bodies verbatim; the default
    replaces anything past 120 characters with a hash, which is what makes
    the snapshots diffable.
    """
    def keep(text):
        if text is None:
            return None
        if hash_long and len(text) > 120:
            return _hashed(text)
        return text

    try:
        f = mutagen.File(path)
    except Exception as e:
        return {"_error": type(e).__name__}
    if f is None:
        return {"_error": "unreadable"}
    out = {}
    info = getattr(f, "info", None)
    out["_len"] = round(getattr(info, "length", 0) or 0, 3)
    out["_bitrate"] = getattr(info, "bitrate", None)
    out["_bytes"] = os.path.getsize(path)
    t = f.tags
    if t is None:
        return out

    if hasattr(t, "getall"):                                       # ID3
        for frame_id, name in _ID3_TEXT.items():
            fr = t.getall(frame_id)
            if fr and getattr(fr[0], "text", None):
                out[name] = keep(_text(fr[0].text))
        # Verbatim desc, never upper-cased. Files that arrived from other
        # taggers carry their own frames: one here holds a real
        # "REPLAYGAIN_TRACK_GAIN" of "-8.80 dB" next to an inherited
        # lower-case "replaygain_track_gain" whose value is the corrupt string
        # "eplaygai". Folding case merged the two and let the junk win,
        # silently, depending on frame order. Muzzi writes its own TXXX descs
        # upper case and its MP4 atoms upper case too, so the containers still
        # line up without any folding.
        for fr in t.getall("TXXX"):
            if fr.text:
                _add(out, fr.desc, keep(_text(fr.text)))
        tlen = t.getall("TLEN")
        if tlen and tlen[0].text:
            out["_TLEN"] = _text(tlen[0].text)
        comm = " ".join(_text(fr.text) for fr in t.getall("COMM") if fr.text)
        out["comment"] = keep(comm) if comm else None
        # Embedded lyrics are one of the two carriers; the .lrc sidecar is the
        # other, and snapshot.py records that separately. Both or neither.
        uslt = t.getall("USLT")
        out["lyrics"] = keep(_text(uslt[0].text)) if uslt and uslt[0].text \
            else None
        apic = t.getall("APIC")
        out["_art"] = _art(apic[0].data, apic[0].mime) if apic else None
    elif isinstance(f, MP4):                                       # MP4
        for atom, name in _MP4_TEXT.items():
            v = t.get(atom)
            if v:
                out[name] = keep(_text(v))
        for atom, name in _MP4_PAIR.items():
            v = t.get(atom)
            if v:
                num, total = (list(v[0]) + [0, 0])[:2]
                out[name] = f"{num}/{total}" if total else str(num)
        for k, v in t.items():
            if k.startswith("----:com.apple.iTunes:") and v:
                _add(out, k.split(":")[-1], keep(_text(v[0])))
        cov = t.get("covr")
        out["_art"] = _art(bytes(cov[0]), "image/jpeg") if cov else None
    else:                                                          # FLAC, OGG
        # Vorbis comments are free-form upper-case keys, which is how
        # write_generic writes them. Reaching this branch through the MP4 one
        # would find no atom it recognises and report an untagged file, so a
        # FLAC would snapshot as empty and every tag on it would be invisible
        # to the diff.
        for k, v in t.items():
            name = _VORBIS_TEXT.get(k.upper(), k.upper())
            _add(out, name, keep(_text(v)))
        pics = getattr(f, "pictures", None)
        out["_art"] = _art(pics[0].data, pics[0].mime) if pics else None
    return out


def _art(data, mime):
    """-> a comparable description of embedded artwork, never the bytes."""
    return (f"{mime} bytes={len(data)} "
            f"sha256:{hashlib.sha256(data).hexdigest()[:16]}")


def sidecar_lrc(audio_path):
    """-> a marker for the .lrc next to an output file, or None.

    Samsung Music reads the sidecar and ignores embedded lyrics, so a change
    that touches one carrier and not the other is a regression on half the
    library's players. This is the field that catches it.
    """
    p = os.path.splitext(audio_path)[0] + ".lrc"
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fh:
        b = fh.read()
    return f"sha256:{hashlib.sha256(b).hexdigest()[:16]} bytes={len(b)}"
