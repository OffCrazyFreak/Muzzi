#!/usr/bin/env python3
"""Read and re-time LRC bodies. No stage of its own -- a library.

Nothing in the pipeline has ever turned an LRC timestamp into a number. Both
existing regexes (write_tags._LRC_STAMP, verify_lyrics._LRC_TS) only strip
stamps out so the words can be language-detected or scored. Trimming leading
silence changes when every event in a file happens, so the timestamps now have
to be arithmetic, and two stages need the same arithmetic to agree.

What real LRC bodies contain, all of which is handled here:

  [ar:Artist]           metadata, left alone
  [offset:+250]         metadata, left alone -- see below
  [00:12.34]line        the common case, centiseconds
  [00:12.345]line       milliseconds
  [00:12]line           no fraction at all
  [00:12.34][01:40.10]  one line, several times it is sung
  <00:12.34>word        enhanced (word-level) LRC, inside a normal line

The offset tag is deliberately NOT folded into the timestamps. Its sign is
read in opposite directions by different players, and Poweramp has a standing
bug report for ignoring it outright, so a body carrying one is already
ambiguous. Rewriting the numbers underneath it would turn one ambiguity into
two. has_offset() reports it instead, and the review sheet surfaces it.
"""
import re

# [mm:ss], [mm:ss.xx], [mm:ss.xxx] -- and the <> form used for word timings.
# Minutes are unbounded because a stamp past [99:59] is legal and does occur.
_STAMP = re.compile(r"([\[<])(\d+):(\d{1,2})(?:([.:])(\d{1,3}))?([\]>])")
_OFFSET = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]\s*$",
                     re.IGNORECASE | re.MULTILINE)


def _seconds(mm, ss, frac):
    """-> a stamp's value in seconds. `frac` keeps its own precision: one
    digit is tenths, two are centiseconds, three are milliseconds.

    The scale comes from the digit count rather than a two-way test, because
    _STAMP accepts one to three digits and reading ".3" as centiseconds turns
    12.3s into 12.03s.
    """
    out = int(mm) * 60 + int(ss)
    if frac:
        out += int(frac) / float(10 ** len(frac))
    return out


def _format(sec, open_ch, sep, digits, close_ch):
    """-> the stamp written back in exactly the shape it arrived in."""
    sec = max(0.0, sec)
    mm, rest = divmod(sec, 60)
    if not digits:
        # Same carry the fractional branch does below: rounding 59.7 up must
        # become the next minute, not ":60".
        whole = int(round(rest))
        if whole >= 60:
            mm, whole = mm + 1, whole - 60
        return f"{open_ch}{int(mm):02d}:{whole:02d}{close_ch}"
    scale = 10 ** digits
    ticks = int(round(rest * scale))
    # Rounding 59.999 up must carry into the minute, not print ":60.00".
    if ticks >= 60 * scale:
        mm, ticks = mm + 1, ticks - 60 * scale
    ss, fr = divmod(ticks, scale)
    return (f"{open_ch}{int(mm):02d}:{int(ss):02d}"
            f"{sep}{fr:0{digits}d}{close_ch}")


def times(text):
    """-> every timestamp in the body, in seconds, in the order written."""
    return [_seconds(m.group(2), m.group(3), m.group(5))
            for m in _STAMP.finditer(text or "")]


def first_time(text):
    """-> the earliest timestamp in seconds, or None for a body with none.

    Earliest, not first written: a line repeated later in the song carries
    several stamps, and files in the wild are not always in time order.
    """
    ts = times(text)
    return min(ts) if ts else None


def lines(text):
    """-> [(seconds, words)] for the timed lines, earliest first.

    A line with several stamps yields one entry per stamp, which is what
    "when is this sung" means. Metadata and untimed lines are dropped, as are
    lines whose only content is a musical-note placeholder.
    """
    out = []
    for raw in (text or "").splitlines():
        stamps = list(_STAMP.finditer(raw))
        if not stamps:
            continue
        head = [m for m in stamps if m.group(1) == "["]
        if not head:
            continue
        # Words are whatever survives once every stamp is removed.
        words = _STAMP.sub("", raw).strip()
        if not words or words in {"♪", "♫", "..", "...", "-"}:
            continue
        for m in head:
            out.append((_seconds(m.group(2), m.group(3), m.group(5)), words))
    return sorted(out, key=lambda p: p[0])


def has_offset(text):
    """-> the [offset:N] tag's value in milliseconds, or None."""
    m = _OFFSET.search(text or "")
    return int(m.group(1)) if m else None


def shift(text, delta):
    """-> the body with every timestamp moved by `delta` seconds.

    Negative results clamp to zero rather than wrapping: a lyric cannot be
    sung before the file starts, and a negative stamp is read as garbage by
    some players and as a huge positive number by others.
    """
    if not text or not delta:
        return text

    def one(m):
        open_ch, mm, ss, sep, frac, close_ch = m.groups()
        digits = len(frac) if frac else 0
        return _format(_seconds(mm, ss, frac) + delta,
                       open_ch, sep or ".", digits, close_ch)

    return _STAMP.sub(one, text)
