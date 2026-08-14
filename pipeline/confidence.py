#!/usr/bin/env python3
"""How much agreement a field actually has, counted in independent families.

`review.score` grades a match on how well one proposal fits the filename. That
answers "is this plausible" and cannot answer "did anything else agree", which
is a different question and often the deciding one. The evidence store holds
every answer every source gave, including the ones that lost, so the second
question is now answerable.

Counted in families, never in sources. Cover Art Archive agreeing with
MusicBrainz is MusicBrainz agreeing with itself, and the file's own tag
agreeing with its filename is one bad download name agreeing with itself.
`evidence.FAMILY` is where that grouping lives.

What this deliberately does NOT do:

Duration is not a constraint here, hard or soft. Gating on it threw away a
third of the correct matches and, reused as a link filter, a fifth of the
answers given by hand, because these files are YouTube rips whose intros the
streaming single does not have.

Nothing here raises a score. Agreement between independent families is real
evidence and will eventually be worth something, but confidence that goes up
moves tracks out of review unseen, and the bar those tracks would cross was
calibrated against a scorer that did not have this. Dissent lowers; agreement
is recorded and left for a change that can be measured on its own.

Usage:
  confidence.py                 # what the store says about the library
  confidence.py --field artist
"""
import argparse
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import evidence  # noqa: E402

# The fields where a wrong value is worse than a missing one, and where a
# disagreement is therefore worth interrupting for. Deliberately not every
# field: sources disagree about `year` constantly, because a reissue date and
# an original release date are both true, and treating that as a contradiction
# would send most of the library to review to answer a question nobody asked.
IDENTITY = ("artist", "title")

# How much a contested identity is discounted. One multiplier, applied once,
# rather than a scale: the honest claim is "something else says otherwise",
# not a measurement of how wrong it is. 0.8 is enough to drop a lone
# catalogue's 0.80 below the review bar without touching a fingerprint match
# or anything a human confirmed.
DISSENT = 0.8

# A dissent from a source that only ever repeats what is already on the file
# is not a second opinion. `local` is the file's own tags, its name and its
# folder, which all descend from the same download.
IGNORE_DISSENT = {"local"}


def _rows(conn, path, field):
    return [r for r in evidence.observations(conn, path, field)
            if r["state"] == evidence.FOUND and r["value_norm"]]


def agreement(conn, path, field):
    """-> {'value', 'agree', 'dissent', 'families', 'human', 'audio'} or None.

    `agree` counts the families behind the most-supported value, `dissent` the
    families behind every other value. A family that says both things counts
    once for agreement and not at all as dissent: a catalogue holding two
    pressings is not arguing with itself.
    """
    rows = _rows(conn, path, field)
    if not rows:
        return None
    by_value = defaultdict(set)
    for r in rows:
        by_value[r["value_norm"]].add(r["family"])
    shown = {r["value_norm"]: r["value"] for r in rows}

    # A value you answered wins outright, however many catalogues prefer
    # another. Ranking on family count alone let two catalogues outvote a
    # hand-given answer, which flagged 28 artists as contested where the only
    # thing contesting them was a database disagreeing with the person who
    # already settled it.
    #
    # Then by families, then by the value's own text, so a tie breaks the same
    # way on every run rather than by dict order.
    def rank(v):
        return (evidence.HUMAN in by_value[v], len(by_value[v]), v)

    top = max(by_value, key=rank)
    agree = by_value[top]
    dissent = set()
    for value, fams in by_value.items():
        if value != top:
            dissent |= fams - agree
    dissent -= IGNORE_DISSENT
    return {"value": shown[top], "agree": sorted(agree),
            "dissent": sorted(dissent),
            "families": sorted(set().union(*by_value.values())),
            "human": evidence.HUMAN in agree,
            "audio": any(r["source"] in evidence.AUDIO_DERIVED
                         for r in rows if r["value_norm"] == top)}


def contested(conn, path, proposed):
    """-> [(field, winning value, dissenting families), ...] worth flagging.

    Only where the proposal itself is the thing being argued with. A field the
    store disagrees about while the pipeline proposed something else again is
    a different and larger problem, and one this is not the place to raise.
    """
    out = []
    for field in IDENTITY:
        want = proposed.get(field)
        if not want:
            continue
        got = agreement(conn, path, field)
        if not got or not got["dissent"]:
            continue
        # A human answer settles it. It outranks every lookup by construction,
        # so a catalogue disagreeing with it is not a reason to ask again.
        if got["human"]:
            continue
        if evidence.norm(want) != evidence.norm(got["value"]):
            continue
        out.append((field, got["value"], got["dissent"]))
    return out


def checked(conn, path, field):
    """-> {'agree', 'dissent', 'silent', 'sources'}: who was asked, who spoke.

    The exhaustion proof behind a review row. `agree` and `dissent` come from
    `agreement`, so they are counted in families; `silent` names the sources
    that were asked and had nothing, and `sources` every source asked at all.

    Silent sources are named individually rather than by family because the
    question they answer is operational, not evidential: "has anyone tried
    Genius for this" is about Genius, and folding it into a family would say
    a lyrics site was consulted when a lyrics site was not.

    A source that was never asked appears nowhere here. Absence in the store
    means the question was not put, which is a different thing from an answer
    of no, and the whole point of the eight states is that the two do not
    collapse into each other.
    """
    rows = evidence.observations(conn, path, field)
    got = agreement(conn, path, field)
    # Silent means the source never answered, not that one of its answers was
    # empty. A source is asked once per question, so a catalogue that missed on
    # the filename and hit on the corrected name has two rows and has plainly
    # answered; reporting "no answer from musicbrainz" next to MusicBrainz's
    # answer is the kind of line that costs the whole column its credibility.
    answered = {r["source"] for r in rows
                if r["state"] == evidence.FOUND and r["value_norm"]}
    silent = sorted({r["source"] for r in rows} - answered)
    return {"agree": (got or {}).get("agree", []),
            "dissent": (got or {}).get("dissent", []),
            "silent": silent,
            "sources": sorted({r["source"] for r in rows})}


def why_review(conn, path):
    """-> a sentence saying what was asked about this track's identity.

    Written for the person opening the spreadsheet, so it names families and
    sources rather than counting them: "which catalogues" is the question, and
    "3" is not an answer to it. Empty when the store knows nothing, which is
    itself readable in the sheet as a blank cell next to a row that has no
    corroboration at all.
    """
    parts = []
    for field in IDENTITY:
        got = checked(conn, path, field)
        if not got["sources"]:
            continue
        said = []
        if got["agree"]:
            said.append(f"{', '.join(got['agree'])} agree")
        if got["dissent"]:
            said.append(f"{', '.join(got['dissent'])} disagree")
        if got["silent"]:
            said.append(f"no answer from {', '.join(got['silent'])}")
        if said:
            parts.append(f"{field}: " + "; ".join(said))
    return " | ".join(parts)


def penalty(conn, path, proposed):
    """-> (multiplier, [reason, ...]) for one track's proposed identity."""
    hits = contested(conn, path, proposed)
    if not hits:
        return 1.0, []
    reasons = [f"{f} contested by {', '.join(fams)}" for f, _v, fams in hits]
    # Applied once however many fields are contested. Two disagreements are
    # one problem with the identity, not two independent halvings.
    return DISSENT, reasons


# ----------------------------------------------------------------- lyrics
#
# A lyric sheet answers two questions and they fail apart. "Are these this
# song's words" is about the text; "were these timings written for this edit"
# is about the numbers. A sheet can be the right song and still be timed
# against a different master, which is the common case rather than the odd
# one: measured here, the median sheet is 0.6s out and 315 of 1178 are more
# than 2s out.
#
# Answering them together is how good words come to certify bad timestamps.
# So they are scored separately, and the words survive when only the numbers
# fail.

# The bar each score has to clear. Both are 0.5 so that a score reads the same
# way whichever half it describes, and so `score >= BAR` is exactly the
# boolean the gates in write_tags have always applied.
LYRIC_TEXT_BAR = 0.5
LYRIC_TIMING_BAR = 0.5

# A timed sheet this long is a person's work against a specific recording, and
# nobody writes one for an instrumental.
MIN_TIMED_LINES = 8


def timed_sheet(entry):
    """-> True when this entry holds a long, human-timed LRC.

    Evidence that the song has words, independent of whether our own model
    could hear them. It matters because "instrumental" here means "under three
    transcribed words", and a transcript can be empty for two very different
    reasons: the track has no vocals, or the model cannot hear this kind of
    music. Croatian and Serbian are tier 3 for Whisper before the singing is
    taken into account, so the second happens a lot.

    Measured on the library: 60 tracks are marked instrumental while a source
    has words for them, 59 of those match their sheet's name at 0.85 or
    better, and 45 hold a synced sheet of 37 to 55 timed lines. Tony
    Cetinski's "Kad Žena Zavoli" is not an instrumental.

    The failure this guards against runs the other way and is left guarded: an
    NCS instrumental was offered 4000 characters of someone else's PLAIN text.
    A wrong match hands over prose, not a forty-line LRC timed to this
    recording, so plain-only entries stay refused.

    Counted with the same parser that writes a sheet out to a file, so "timed
    line" means here what it means there. Counting non-empty lines instead
    would let plain prose that happened to land in the `synced` field clear
    the bar, which is the exact hole this discriminator exists to close: a
    wrong match hands over prose, and prose has line breaks too.
    """
    if not isinstance(entry, dict):
        return False
    from pipeline import lrc
    return len(lrc.lines(entry.get("synced") or "")) >= MIN_TIMED_LINES


def lyric_text(entry, verified=None, artist=None, title=None):
    """-> (score 0..1, reason). How much these words are this song's.

    The reasons are the ones write_tags has always given, kept verbatim so a
    review sheet reads the same. What is new is that a pass carries a number:
    a sheet matched at 0.55 was as good as one matched at 1.00 before, and
    only the boolean survived to say otherwise.

    Scored, in order:
      instrumental               0.0   the track has no words at all
      confirmed by the audio     1.0   the transcript settles it
      no recorded match          0.0   absence of evidence is not agreement
      otherwise      min(artist fit, title fit)
    """
    from pipeline.webmatch import MIN_FIT, fit
    if not entry or not isinstance(entry, dict):
        return 1.0, None                 # nothing was offered, nothing to doubt
    if verified and verified.get("instrumental") and not timed_sheet(entry):
        return 0.0, "instrumental"
    confirmed = bool(verified and verified.get("verdict") == "confirmed")
    matched = entry.get("matched") or ""
    if " - " not in matched:
        if entry.get("synced") or entry.get("plain"):
            return (1.0, None) if confirmed else (0.0, "unverifiable_match")
        return 1.0, None
    ma, _, mt = matched.partition(" - ")
    # Prefer the fits the picker already stamped; fall back to computing them
    # so an entry written by an older selector is still judged.
    af, tf = entry.get("artist_fit"), entry.get("title_fit")
    if artist and af is None:
        af = fit(artist, ma)
    if title and tf is None:
        tf = fit(title, mt)
    if artist and af is not None and af < MIN_FIT:
        return (1.0, None) if confirmed else (0.0, "wrong_artist")
    if title and tf is not None and tf < MIN_FIT:
        return (1.0, None) if confirmed else (0.0, "wrong_title")
    # Only the halves that were actually checked. A fit is stamped on the
    # entry whether or not the caller supplied the name to compare it with,
    # and scoring an unchecked one would fail a sheet on a comparison nobody
    # made: a stamped title_fit of 0.0 with no title to check against means
    # "not measured", not "wrong".
    got = [x for want, x in ((artist, af), (title, tf))
           if want and x is not None]
    return (min(got) if got else 1.0), None


def lyric_timing(entry, decoded_secs=None, cut=0.0):
    """-> (score 0..1 or None, reason). How much the timings fit this file.

    None means unknown rather than good: a sheet that publishes no duration,
    like Deezer's, cannot be judged here and is not evidence of anything. The
    caller treats it as passing, which is what has always happened, but the
    two are worth telling apart when reporting.

    Below the bar exactly when the drift exceeds the tolerance the gate has
    always used, so a passing sheet keeps its timings and a failing one keeps
    its words.

    `cut` is subtracted because `decoded_secs` describes the untrimmed source
    while the copy we ship is `cut` seconds shorter, and the sheet was timed
    against a master with no padding at all.
    """
    from pipeline.webmatch import MAX_DURATION_DELTA
    md = entry.get("matched_duration") if isinstance(entry, dict) else None
    if not md or not decoded_secs:
        return None, None
    drift = abs((decoded_secs - (cut or 0.0)) - md)
    if drift > MAX_DURATION_DELTA:
        return 0.0, "duration_mismatch"
    # Linear from 1.0 at no drift to the bar at the tolerance, so a sheet 1.9s
    # out is visibly weaker than one 0.1s out while both still pass.
    return 1.0 - (drift / MAX_DURATION_DELTA) * (1.0 - LYRIC_TIMING_BAR), None


# ------------------------------------------------------- lyrics vs the cut
#
# Trimming the end of a file removes audio that was measured as silent. A
# synced lyric sheet that still has words in there is not a small discrepancy:
# one of the two is wrong about this recording, and cutting would settle the
# argument by destroying the evidence.

def tail_conflict(entry, decoded_secs, tail_cut, verified=None,
                  artist=None, title=None, head_cut=0.0):
    """-> (safe, reason) for cutting `tail_cut` seconds off this file's end.

    Not "does the sheet look right", which `lyric_text` answers. This asks the
    narrower question the cut depends on: is anything sung inside the stretch
    about to be removed.

    Resolved without asking wherever the answer is already known:

      no timed sheet          nothing can contradict the cut
      last sung line is
        before the cut        the cut removes silence, as measured
      the sheet's timings
        are already refused   write_tags drops them and keeps the words, so
                              no timestamp survives to be wrong about
      otherwise               a real contradiction, and one for you

    The last case is the whole point. A sheet whose timings this pipeline
    trusts, placing words inside audio this pipeline measured as silent, means
    one of two measurements is wrong, and there is no way to tell which from
    here. Cutting anyway would be picking the answer that deletes something.
    """
    if tail_cut <= 0 or not decoded_secs:
        return True, None
    if not isinstance(entry, dict):
        return True, None
    from pipeline import lrc
    sung = lrc.lines(entry.get("synced") or "")
    if not sung:
        return True, None
    # Where the removed stretch begins, on the source's own timeline. A head
    # trim shifts the sheet later, but it shifts the audio with it, so the two
    # stay in step and this comparison needs no adjustment.
    cut_start = decoded_secs - tail_cut
    last = sung[-1][0]
    if last < cut_start:
        return True, None
    # A sheet whose timings are refused contributes no timestamps to the
    # output at all: write_tags keeps its words and drops its numbers. There
    # is nothing left for the cut to contradict.
    # With the same head cut write_tags judges it against. Asking a different
    # question here than the one that decides the file's actual timings is how
    # this ends up believing the numbers were dropped when they were kept, and
    # cutting on the strength of it.
    timing, _why = lyric_timing(entry, decoded_secs, head_cut)
    if timing is not None and timing < LYRIC_TIMING_BAR:
        return True, "timings already dropped"
    text, why = lyric_text(entry, verified, artist, title)
    if text < LYRIC_TEXT_BAR:
        return True, f"sheet already refused ({why})"
    return False, (f"a line is sung at {last:.1f}s, inside the {tail_cut:.1f}s "
                   f"about to be cut from the end")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--field", action="append",
                    help="only these fields (repeatable)")
    ap.add_argument("--db", default=evidence.DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no store at {args.db}; run "
                 f"pipeline/evidence.py --backfill first")
    conn = evidence.connect(args.db, readonly=True)
    fields = args.field or list(IDENTITY)

    print()
    for field in fields:
        rows = conn.execute(
            "SELECT DISTINCT track_path FROM observation WHERE field=?",
            (field,)).fetchall()
        contested_n, families = 0, defaultdict(int)
        for (path,) in rows:
            got = agreement(conn, path, field)
            if got and got["dissent"]:
                contested_n += 1
                for f in got["dissent"]:
                    families[f] += 1
        print(f"  {field}: {contested_n} of {len(rows)} tracks contested")
        for f, n in sorted(families.items(), key=lambda kv: -kv[1]):
            print(f"    dissenting family {f:14s} {n}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
