#!/usr/bin/env python3
"""Stage 4c: score confidence and build a worst-first review queue.

The premise: a wrong tag is worse than a missing one. So instead of accepting
whatever the matcher returns, every track gets a confidence score and an
explicit list of reasons. Anything below the auto-accept bar lands in a queue
sorted least-confident-first, so an hour of review fixes the most likely
mistakes rather than a random sample.

The queue doubles as the input format: fill in the `hint` column and re-run.
A hint can be a YouTube URL, or `artist=...; title=...`.

Usage: review.py [--auto-thresh 0.85] [--review-thresh 0.55]
"""
import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from pipeline import confidence, evidence  # noqa: E402
from pipeline.fingerprint import likely_balkan  # noqa: E402
from pipeline.probe_match import split_name  # noqa: E402,F401
from pipeline import tagseed  # noqa: E402
from pipeline import bpm_overrides  # noqa: E402

IDENT = os.path.join(HERE, "cache", "identity.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
TEXTSEARCH = os.path.join(HERE, "cache", "textsearch.json")
HINTS = os.path.join(HERE, "hints.tsv")
# Working files, not things to open: they live in cache/ so the project root
# holds only what actually wants your attention.
QUEUE = os.path.join(HERE, "cache", "review_verify.tsv")
UNKNOWN_QUEUE = os.path.join(HERE, "cache", "review_unknown.tsv")
# webmatch.py's single-source (grade B) queue: same hint format, so it is just
# another file to read hints out of.
CONFIRM_QUEUE = os.path.join(HERE, "review_confirm.tsv")
# The tracks no catalogue could corroborate at all -- the only queue that
# genuinely needs a link rather than a yes or no.
UNFOUND_QUEUE = os.path.join(HERE, "review_unfound.tsv")

# Everything you are asked to look at lives in one folder and is rebuilt from
# scratch each run. Old sheets used to pile up in the project root with no way
# to tell which had been done, so "107 rows need review" pointed at nothing in
# particular. They are numbered in the order worth working through.
REVIEW_DIR = os.path.join(HERE, "review")
SHEETS = [
    ("1 - needs a link", "unknown",
     "Nothing was found anywhere. Paste a YouTube (or any) link in `hint`."),
    ("2 - confirm the name", "confirm",
     "One catalogue agreed and another did not. Type y or n in `hint`."),
    ("3 - check the rest", "verify",
     "Everything else, least confident first. Only the top rows need you."),
    ("4 - trim and lyric timing", "trim",
     "Timing and lyric fit, not identity. Nothing here is wrong yet: these "
     "are the files where the automatic decision was declined or could not be "
     "checked. Fixable rows come first, the rest are recorded so the counts "
     "add up. In `hint` type trim=n to keep a file at full length, or trim=y "
     "to cut the silence anyway. Leave it blank to change nothing."),
]
OUT = os.path.join(HERE, "cache", "review.json")
DUPES = os.path.join(HERE, "cache", "duplicates.json")
YT_HINTS = os.path.join(HERE, "cache", "hint_youtube.json")
FN_GUESS = os.path.join(HERE, "cache", "filename_guess.json")
WEBMATCH = os.path.join(HERE, "cache", "webmatch.json")
SILENCE = os.path.join(HERE, "cache", "silence.json")
ALIGN = os.path.join(HERE, "cache", "lyric_align.json")
LYRICS = os.path.join(HERE, "cache", "lyrics.json")
LYRIC_VERIFY = os.path.join(HERE, "cache", "lyric_verify.json")


def _load(path):
    """-> a cache written by a stage that may not have run yet."""
    return json.load(open(path)) if os.path.exists(path) else {}


def _hints_from_ods(path, column="hint"):
    """Read an edited LibreOffice spreadsheet directly, so there is no export
    step to get wrong.

    column names which answer to take. yt_links.py asks about links in a
    column of its own, because a bare "y" in a hint column means the artist
    and title are right, which is not what its sheet asked.
    """
    try:
        from odf.opendocument import load
        from odf.table import Table, TableRow, TableCell
        from odf.text import P
    except ImportError:
        return {}
    out, doc = {}, load(path)
    for table in doc.getElementsByType(Table):
        header, rows = None, []
        for tr in table.getElementsByType(TableRow):
            cells = []
            for tc in tr.getElementsByType(TableCell):
                txt = " ".join(str(p) for p in tc.getElementsByType(P))
                rep = int(tc.getAttribute("numbercolumnsrepeated") or 1)
                cells.extend([txt] * min(rep, 32))
            # Find the header rather than assuming it is the first row: the
            # sheets carry a one-line instruction above it, and a note row
            # mistaken for the header means every answer below is dropped in
            # silence.
            if header is None:
                if "file" in [c.strip() for c in cells]:
                    header = [c.strip() for c in cells]
            else:
                rows.append(cells)
        if not header or "file" not in header:
            continue
        fi, hi = header.index("file"), (header.index(column)
                                        if column in header else None)
        for r in rows:
            if hi is None or len(r) <= max(fi, hi):
                continue
            name, hint = r[fi].strip(), r[hi].strip()
            if name and hint:
                out[name] = hint
    return out


def load_hints():
    """filename -> hint string. The review queue is itself a valid hints file.

    Accepts tab, comma or semicolon separation, and .csv/.tsv/.txt alike.
    LibreOffice has no "TSV" save option -- it writes "Text CSV", and which
    delimiter comes out depends on the export dialog and the locale (a Croatian
    locale defaults to semicolons). Sniffing the delimiter means whatever gets
    saved just works, instead of silently loading zero hints.
    """
    hints = {}
    seen = set()
    candidates = [HINTS, QUEUE, UNKNOWN_QUEUE, CONFIRM_QUEUE, UNFOUND_QUEUE]
    # Where the queues used to live, so answers given before the reorganisation
    # are still found and folded into hints.tsv rather than quietly lost.
    candidates += [os.path.join(HERE, n) for n in
                   ("review_verify.tsv", "review_unknown.tsv")]
    for base in (QUEUE, UNKNOWN_QUEUE, CONFIRM_QUEUE, UNFOUND_QUEUE, HINTS):
        stem = os.path.splitext(base)[0]
        candidates += [stem + ext for ext in (".csv", ".txt", ".tsv")]
    for base in (QUEUE, UNKNOWN_QUEUE, CONFIRM_QUEUE, UNFOUND_QUEUE):
        candidates.append(os.path.splitext(base)[0] + ".ods")
    # The numbered sheets, plus anything you saved beside them under another
    # name. Read last so a fresh answer beats a stale one from the old layout.
    if os.path.isdir(REVIEW_DIR):
        candidates += sorted(
            os.path.join(REVIEW_DIR, n) for n in os.listdir(REVIEW_DIR)
            if n.lower().endswith((".ods", ".tsv", ".csv", ".txt")))
    for path in candidates:
        if path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        if path.endswith(".ods"):
            for k, v in _hints_from_ods(path).items():
                _keep(hints, k, v)
            continue
        with open(path, encoding="utf-8-sig", newline="") as fh:
            head = fh.readline()
            if not head:
                continue
            delim = max(("\t", ",", ";"), key=head.count)
            if head.count(delim) == 0:
                continue
            fh.seek(0)
            for row in csv.DictReader(fh, delimiter=delim):
                name = (row.get("file") or "").strip()
                hint = (row.get("hint") or "").strip()
                if name and hint:
                    _keep(hints, name, hint)
    return hints


def _keep(hints, name, hint):
    """Later files win, except that a note never displaces a real answer.

    The sheets are read after hints.tsv so that a fresh answer overrides a
    stale one. But a note is not an answer: a link typed today was being
    thrown away by "same as above, forgot to merge" left in a sheet from
    before, purely because the sheet was read second.
    """
    old = hints.get(name)
    if old and parse_hint(hint)[0] == "note" and parse_hint(old)[0] != "note":
        return
    hints[name] = hint


def adopt_orphan_hints(hints, present):
    """Move an answer off a file that no longer exists and onto its surviving
    twin.

    Answers are stored per filename on purpose -- a filename never moves,
    whereas the proposed artist and title do, and keying on those once let a
    link typed against one song jump onto an unrelated one. But deleting a
    duplicate then strands the answer that was typed on it: confirming
    "Blackbear is the artist" on the copy that later lost the dedupe left the
    survivor credited to a lyrics channel again.

    Match filenames the way dedupe_names does -- one is a suffix of the other,
    because the pair is usually "Channel Name - Song" and "Song".
    """
    have = set(present)
    orphans = {k: v for k, v in hints.items() if k not in have}
    if not orphans:
        return 0
    keys = {n: _norm(os.path.splitext(n)[0]) for n in have}
    moved = 0
    for name, hint in orphans.items():
        ok = _norm(os.path.splitext(name)[0])
        if not ok:
            continue
        # Longest match wins, so "idfc x soap" prefers the closest survivor
        # rather than the first arbitrary containment.
        cands = [n for n, k in keys.items() if k and (k in ok or ok in k)]
        if not cands:
            continue
        best = max(cands, key=lambda n: len(keys[n]))
        # A note is a remark, not an answer. "same as the one above, you
        # didn't merge" sitting on the survivor blocked the plain `y` typed on
        # its twin, so the track shipped named after a lyrics channel.
        current = hints.get(best, "")
        if not current or (parse_hint(current)[0] == "note"
                           and parse_hint(hint)[0] != "note"):
            hints[best] = hint
            moved += 1
            continue
        # The survivor already has an answer, so do not overwrite it -- but a
        # parenthetical note is extra information, not a competing answer.
        # "(Blackbear is the artist)" was typed once, on the copy that later
        # lost the dedupe, and the survivor kept being credited to a lyrics
        # channel because its own answer was a bare link.
        note = re.search(r"\(([^()]{3,80})\)\s*$", hint)
        if note and note.group(1).lower() not in hints[best].lower():
            hints[best] = f"{hints[best]} ({note.group(1)})"
            moved += 1
    if moved:
        print(f"  {moved} answers moved onto the surviving copy of their song")
    return moved


def load_links():
    """-> {filename: link}, the 'link' column of hints.tsv.

    A column of its own, because the answers mean different things: "y" in the
    hint column confirms an artist and title, and yt_links.py is asking which
    video a file came from.
    """
    out = {}
    if not os.path.exists(HINTS):
        return out
    with open(HINTS, encoding="utf-8-sig", newline="") as fh:
        head = fh.readline()
        if not head:
            return out
        fh.seek(0)
        for row in csv.DictReader(fh, delimiter=max(("\t", ",", ";"),
                                                    key=head.count)):
            name = (row.get("file") or "").strip()
            link = (row.get("link") or "").strip()
            if name and link:
                out[name] = link
    return out


def save_hints(hints, links=None):
    """The durable record of everything you have answered.

    Until now an answer existed only inside whichever spreadsheet you typed it
    into, so regenerating the sheets would have thrown away every y, n and link
    -- 1551 of them. The sheets are a view; this file is the memory.

    The link column is read back and re-emitted even when this stage knows
    nothing about it: review.py rewrites the whole file, so a column it does
    not carry is a column it deletes.
    """
    links = load_links() if links is None else links
    with open(HINTS, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        if links:
            w.writerow(["file", "hint", "link"])
            for name in sorted(set(hints) | set(links)):
                w.writerow([name, hints.get(name, ""), links.get(name, "")])
            return
        w.writerow(["file", "hint"])
        for name in sorted(hints):
            w.writerow([name, hints[name]])


_CONTRADICTED = ("mismatch vs filename", "differs from filename")


def sheet_groups(rows):
    """Split what needs you by the kind of answer it needs, not by which
    stage happened to produce it.

    A row with no name at all cannot be confirmed, only supplied -- it needs a
    link. A row whose proposed name contradicts the filename is a yes or no.
    Everything else is a skim. Grouping by stage instead meant one queue asked
    for three different kinds of answer, and the sheets only existed if an
    earlier stage had left its file lying around.
    """
    needs_link, confirm, rest = [], [], []
    for r in rows:
        if r["tier"] == "auto":
            continue
        if not (r.get("proposed_artist") and r.get("proposed_title")):
            needs_link.append(r)
        elif any(k in why for why in r["reasons"] for k in _CONTRADICTED):
            confirm.append(r)
        else:
            rest.append(r)
    key = lambda r: r["confidence"]          # noqa: E731  worst first
    return {"unknown": sorted(needs_link, key=key),
            "confirm": sorted(confirm, key=key),
            "verify": sorted(rest, key=key),
            "trim": timing_rows(rows)}


# A trim this small is not worth anyone's attention even when its lyric
# alignment could not be checked.
_TRIM_WORTH_CHECKING = 0.5

# What a hint resolved from a pasted SEARCH is worth. Below --auto-thresh
# (0.90) so the row lands in the review queue, and above --review-thresh
# (0.55) so it sorts as a strong candidate rather than a doubtful one: the
# first result of a search you typed is usually right, it is just not a thing
# you confirmed.
SEARCH_HINT_CONF = 0.85


def timing_rows(rows):
    """-> the rows whose audio timing or lyric timing needs a human.

    Deliberately built from ALL rows, not from what sheet_groups filtered.
    Identity and timing are unrelated problems: a track can be identified with
    total confidence and still open with eight seconds of dead air, or carry a
    lyric sheet written for a different recording.

    Four kinds of row, worst first:

      * head silence too long to cut blind -- almost always a quiet intro
        rather than dead air, and cutting it would decapitate the song
      * the lyric sheet's duration disagrees with the file by more than ten
        seconds, so it belongs to another recording and no offset can fix it
      * write_tags refused the sheet, or refused its timings
      * trimmed, but the lyric alignment could not be measured

    The refusals are here rather than in a sheet of their own because they are
    the same question: is this sheet describing this recording. lyric_align
    files a row at ten seconds out; write_tags refuses the timings at two, so
    without this the tracks between those thresholds went quiet with nothing
    to look at.

    The gates are imported rather than restated. A copy here would keep
    reporting after write_tags changed its mind, which is the failure the
    audit tools avoid the same way.

    A cut that was wanted and did not happen is not here: write_tags counts it
    in stats["trim_failed"] and writes no cache, so this sheet cannot see it.
    """
    from pipeline.write_tags import lyrics_trustworthy, lyrics_timing_ok
    from pipeline import silence

    sil = _load(SILENCE)
    align = _load(ALIGN)
    lyr = _load(LYRICS)
    verified = {v.get("path"): v for v in _load(LYRIC_VERIFY).values()
                if isinstance(v, dict) and v.get("path")}
    secs = {}
    for v in _load(ANALYSIS).values():
        if v.get("path"):
            secs[v["path"]] = v.get("decoded_secs")
    # Said plainly, because "wrong_artist" in a spreadsheet cell is not an
    # instruction. Each one names what a human would actually do about it.
    _SAY = {
        "wrong_artist": "lyrics are by a different artist; if it is the same "
                        "performer under another name, add it to "
                        "config/artist_aliases.json",
        "wrong_title": "lyrics are a different song by the right artist",
        "unverifiable_match": "lyrics predate the match record, so nothing "
                              "says which song they belong to",
        "duration_mismatch": "lyric sheet was timed for a different edit, so "
                             "the words are kept and the timings dropped",
        "instrumental": "no words found in the audio, but lyrics were fetched",
    }
    out = []
    for r in rows:
        p = r.get("path")
        s, a = sil.get(p) or {}, align.get(p) or {}
        why, lyric_reason = [], None
        if s.get("hinted") is not None:
            continue                     # you already answered this one
        if s.get("decision") == "review_long":
            why.append(f"opens with {s['lead']:.1f}s of near-silence, too much "
                       f"to cut without looking")
        if a.get("status") == "wrong_recording":
            why.append(f"lyric sheet is {abs(a['duration_delta']):.0f}s off this "
                       f"recording's length -- wrong version")
        elif (s.get("decision") == "trim"
              and s.get("cut", 0) >= _TRIM_WORTH_CHECKING
              and a and a.get("status") != "measured"):
            why.append(f"cutting {s['cut']:.1f}s, lyric timing unverified "
                       f"({a.get('status')})")
        art, tit = r.get("proposed_artist"), r.get("proposed_title")
        entry = lyr.get(f"{art}|{tit}".lower()) if (art and tit) else None
        # A tail cut that a lyric sheet contradicts. write_tags declines to
        # make it and there is no other way to find out: the file simply keeps
        # its dead air, and the reason it keeps it is the interesting part.
        tail = silence.tail_cut_for(s)
        if tail:
            ok, tail_why = confidence.tail_conflict(
                entry, secs.get(p), tail, verified.get(p), art, tit,
                silence.cut_for(s))
            if not ok:
                why.append(f"{tail_why}, so the {tail:.1f}s was left alone; "
                           f"trim=y cuts it anyway, trim=n keeps it for good")
        if isinstance(entry, dict) and (entry.get("synced") or entry.get("plain")):
            ok, reason = lyrics_trustworthy(entry, verified.get(p), art, tit)
            if ok and entry.get("synced"):
                # The cut write_tags applies, not the one merely recorded: a
                # review_long row carries a figure that is never taken, and
                # judging against it invents refusals write_tags never made.
                # Importing the gate is not enough while the input differs.
                ok, reason = lyrics_timing_ok(entry, secs.get(p),
                                              silence.cut_for(s))
            # The ten-second rows above already say this, in more detail.
            if not ok and a.get("status") != "wrong_recording":
                why.append(f"{_SAY.get(reason, reason)} "
                           f"(matched {entry.get('matched') or 'nothing'})")
                lyric_reason = reason
        if not why:
            continue
        row = dict(r)
        row["reasons"] = why
        # The code, not the sentence. The sort below used to search the prose
        # in _SAY, so rewording a cell silently reordered the sheet.
        row["lyric_reason"] = lyric_reason
        out.append(row)
    # Actionable first, then longest silence, then biggest version mismatch:
    # the rows where doing nothing costs the most, and where doing something
    # is possible at all.
    #
    # The ordering matters more than it used to. Refused timings outnumber
    # every other kind of row roughly five to one, and they are the one kind a
    # human cannot fix: the words are already kept and no answer changes that.
    # Sorted by size alone they would bury the few dozen rows where naming an
    # alias actually repairs a track.
    def worst(r):
        p = r.get("path")
        s, a = sil.get(p) or {}, align.get(p) or {}
        fixable = r.get("lyric_reason") in ("wrong_artist", "wrong_title")
        return (0 if fixable else 1,
                -(max(s.get("lead") or 0,
                      abs((a.get("duration_delta") or 0)) / 10.0)))
    return sorted(out, key=worst)


def _rows_from(queue_path, rows):
    """-> the rows named by another stage's queue file (webmatch writes one)."""
    if not os.path.exists(queue_path):
        return []
    by_file = {r["file"]: r for r in rows}
    out = []
    with open(queue_path, encoding="utf-8-sig", newline="") as fh:
        head = fh.readline()
        if not head:
            return []
        delim = max(("\t", ",", ";"), key=head.count)
        fh.seek(0)
        for rec in csv.DictReader(fh, delimiter=delim):
            r = by_file.get((rec.get("file") or "").strip())
            if r is not None:
                out.append(r)
    return out


def _flat(value):
    """-> a cell of links written out as text, for the .tsv fallback."""
    if isinstance(value, list):
        return "; ".join(f"{label}: {url}" for label, url in value)
    return value


def _write_cell(cell, value):
    """Fill one spreadsheet cell, turning links into links.

    A cell may be a list of (label, url) pairs. Each becomes a real hyperlink
    with the label as its text, so the dossier column reads as a row of names
    to click rather than four hundred characters of URL. Anything else is
    written as before, except that a bare http token inside it is linkified
    too: sheet 1 and the yt_links sheet have always held URLs as plain text,
    and clicking one meant copying it out by hand.
    """
    from odf.text import A, P
    p = P()
    if isinstance(value, list):
        for n, (label, url) in enumerate(value):
            if n:
                p.addText("  |  ")
            p.addElement(A(href=url, text=label))
        cell.addElement(p)
        return
    parts = str(value).split(" ")
    for n, word in enumerate(parts):
        if n:
            p.addText(" ")
        if word.startswith(("http://", "https://")):
            p.addElement(A(href=word, text=word))
        else:
            p.addText(word)
    cell.addElement(p)


def _ods(path, title, note, items, with_proposal=True, cols=None, cells=None):
    """Write one spreadsheet. Falls back to .tsv if odfpy is missing.

    cols/cells override the identity-queue layout, so another stage can publish
    a sheet of its own without a second copy of this writer. yt_links.py uses
    that to ask about links.
    """
    if cols is None:
        cols = ["rank", "confidence", "tier", "file", "proposed_artist",
                "proposed_title", "proposed_album", "proposed_year", "why",
                "checked", "look here", "audio_flags", "hint"]

    if cells is None:
        def cells(r, i):
            return [i, r["confidence"], r["tier"], r["file"],
                    (r["proposed_artist"] or "") if with_proposal else "",
                    (r["proposed_title"] or "") if with_proposal else "",
                    (r["proposed_album"] or "") if with_proposal else "",
                    (r["proposed_year"] or "") if with_proposal else "",
                    "; ".join(r["reasons"]), r.get("checked", ""),
                    r.get("links") or "",
                    "; ".join(r["flags"]), r["hint"]]
    try:
        from odf.opendocument import OpenDocumentSpreadsheet
        from odf.table import Table, TableRow, TableCell
    except ImportError:
        with open(os.path.splitext(path)[0] + ".tsv", "w",
                  encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(cols)
            for i, r in enumerate(items, 1):
                w.writerow([_flat(v) for v in cells(r, i)])
        return

    doc = OpenDocumentSpreadsheet()
    tbl = Table(name=title[:31])

    def row(values, header=False):
        tr = TableRow()
        for v in values:
            # Numbers as numbers, so sorting the sheet by confidence in
            # LibreOffice does not order them as text (0.9 above 0.85).
            if not header and isinstance(v, (int, float)):
                c = TableCell(valuetype="float", value=float(v))
            else:
                c = TableCell(valuetype="string")
            _write_cell(c, v)
            tr.addElement(c)
        tbl.addElement(tr)

    row([note] + [""] * (len(cols) - 1), header=True)
    row(cols, header=True)
    for i, r in enumerate(items, 1):
        row(cells(r, i))
    doc.spreadsheet.addElement(tbl)
    doc.save(path)


def links_from_sheets():
    """-> {filename: link} typed into the review sheets since the last run.

    hints.tsv is the memory; the sheets are a view. A link answer only exists
    in the sheet until something copies it across, and this stage deletes every
    sheet a few lines below. review runs five times before yt_links does, so
    without this the answer is wiped in the same run it was given, and the user
    sees a question they already answered come back.

    Read from the "link" column specifically: sheet 4 asks for a URL, and a
    bare "y" in a hint column means the artist and title are right, which is a
    different question.
    """
    out = {}
    if not os.path.isdir(REVIEW_DIR):
        return out
    for n in sorted(os.listdir(REVIEW_DIR)):
        if n.lower().endswith(".ods"):
            out.update(_hints_from_ods(os.path.join(REVIEW_DIR, n),
                                       column="link"))
    return {k: v for k, v in out.items() if v}


def _search_terms(row):
    """-> (artist, title) to search catalogues for, falling back to the name.

    A row on sheet 1 has no proposed identity by definition, and those are the
    rows where a search link is worth most. The filename is what a person would
    paste in themselves, minus the extension and the leading track number.
    """
    if row.get("proposed_artist") or row.get("proposed_title"):
        return row.get("proposed_artist"), row.get("proposed_title")
    stem = os.path.splitext(row.get("file") or "")[0]
    return None, re.sub(r"^\s*\d{1,3}\s*[-.]\s*", "", stem).strip()


def attach_dossier(rows, db=None):
    """Add `checked` and `links` to every row that a person will be shown.

    Two columns answering the two questions a reviewer actually has: what has
    already been asked, and where do I go to settle it. Both are read out of
    the evidence store, so neither costs a request and neither can invent
    anything: a link is built from an identifier a source returned, and the
    `checked` sentence names the families that answered.

    Silent on failure. The store is a record of what happened, and a review
    sheet that cannot be written because the record was unreadable is a worse
    outcome than a sheet with two empty columns.
    """
    from pipeline import links
    try:
        conn = evidence.connect(db or evidence.DB, readonly=True)
    except Exception:
        return rows
    for r in rows:
        path = r.get("path")
        if not path:
            continue
        try:
            artist, title = _search_terms(r)
            # Both, then assign. Setting `checked` first and letting the
            # dossier throw would leave a row saying what was asked with no
            # way to go and look, which reads as "there is nowhere to check".
            checked = confidence.why_review(conn, path)
            # Every site, not only the ones that were asked. Offering the
            # asked-only subset was the first shape of this and it read
            # backwards: a row is in review precisely because what was asked
            # did not settle it, so the sites worth naming are the ones nobody
            # has tried. Which ones were asked is the `checked` column's job.
            found = links.dossier(conn, path, artist, title, links.SEARCH)
        except Exception:
            continue
        r["checked"], r["links"] = checked, found
    return rows


def publish_sheets(groups, hints):
    """Rebuild the review folder from scratch, numbered in working order."""
    # Before the wipe, or the answer goes with the sheet.
    links = load_links()
    links.update(links_from_sheets())
    save_hints(hints, links=links)
    os.makedirs(REVIEW_DIR, exist_ok=True)
    # Wipe first. A sheet that is not regenerated is a sheet with nothing left
    # to answer, and leaving it on disk is what made it impossible to tell
    # which ones were still outstanding.
    for n in os.listdir(REVIEW_DIR):
        if n.lower().endswith((".ods", ".tsv", ".csv", ".txt")):
            os.remove(os.path.join(REVIEW_DIR, n))
    # The old flat layout, now superseded.
    for stale in (QUEUE, UNKNOWN_QUEUE, CONFIRM_QUEUE, UNFOUND_QUEUE):
        for ext in (".ods", ".csv", ".txt"):
            p = os.path.splitext(stale)[0] + ext
            if os.path.exists(p):
                os.remove(p)

    made = []
    for number, (label, key, note) in enumerate(SHEETS, 1):
        items = [r for r in groups.get(key) or [] if r["tier"] != "auto"]
        if not items:
            continue
        attach_dossier(items)
        path = os.path.join(REVIEW_DIR, f"{label}.ods")
        _ods(path, label, note, items, with_proposal=(key != "unknown"))
        made.append((label, len(items)))
    print(f"\n  review sheets -> {REVIEW_DIR}")
    if made:
        for label, n in made:
            print(f"    {label:24} {n:4} rows")
    else:
        print("    nothing left to review")
    print(f"  answers remembered in {os.path.basename(HINTS)} "
          f"({len(hints)} so far), so the sheets can be deleted freely\n")


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# How alike two names have to be before a link counts as confirming the match
# it replaced rather than replacing it. "EDEN - circles" against "EDEN -
# Circles" is a spelling, so the recording ID survives; "Ensemble
# Pittoresque - Ontwaakt" against "Mile Kitic - Sampion" is a different song
# and everything the old match knew about it goes with it. Deliberately high:
# keeping a wrong MBID is the failure this exists to prevent, and re-enriching
# a right one costs one lookup.
SAME_RECORDING = 0.92


def same_recording(old, artist, title):
    """-> True when a hint only respells the match it is replacing.

    Compared as one string, artist and title together, because a link that
    changes only the artist ("Gospoda" for "Elitni Odredi") is exactly the
    case where the old album, year and recording ID must not be inherited.
    """
    was = f"{old.get('artist') or ''} {old.get('title') or ''}".strip()
    now = f"{artist or ''} {title or ''}".strip()
    if not was or not now:
        return False
    a, b = _norm(was), _norm(now)
    return bool(a) and bool(b) and SequenceMatcher(None, a, b).ratio() >= SAME_RECORDING


# What a match knows only because of the identity it was matched to. When the
# identity changes, these describe the song that was rejected.
IDENTITY_FIELDS = ("recording_id", "release", "cover_url", "isrc",
                   "fingerprint_score", "artist_similarity",
                   "title_similarity", "combined")


def rename_match(m, rel, artist, title, recording_id=None):
    """-> (match, release, kept) with the answer's name on it.

    One boundary, however the name was changed: a link, or a name you typed.
    Both replace the artist and title on a match that was found by other
    means, and everything that match knew about the album, the recording and
    the artwork was true of the song it found, not of the one you named.

    `kept` is False when those facts have been cleared. Two ways to lose them:
    the names describe a different song, or the answer carries a recording ID
    that disagrees with the one already there. The second matters even when
    the names agree, because taking the new ID while keeping the old ISRC and
    album hands cascade.py a seed assembled from two recordings.
    """
    m = dict(m or {})
    kept = same_recording(m, artist, title)
    if kept and recording_id and m.get("recording_id") \
            and m["recording_id"] != recording_id:
        kept = False
    if not kept:
        for k in IDENTITY_FIELDS:
            m.pop(k, None)
        rel = {}
    m["artist"], m["title"] = artist, title
    return m, dict(rel or {}), kept


def _songkey(artist, title, filename):
    """Identity of the SONG, independent of which file it came from."""
    base = f"{artist or ''} {title or ''}".strip() or os.path.splitext(filename)[0]
    return _norm(base)


def _filekey(filename):
    """Second grouping key, from the filename alone.

    The proposal key is not enough: when the SAME song is matched wrongly in
    two different ways (one copy "Infernal Legions Of Mordor", the other
    "La'Porsha Renae"), the proposals disagree and a hint on one would not
    reach the other. The filenames still agree, so they group here.
    """
    stem = os.path.splitext(filename)[0]
    a, t = split_name(stem)
    return _norm(f"{a or ''} {t or ''}") or _norm(stem)



# What may go in the `hint` column.
_YES = {"y", "yes", "ok", "correct", "da", "+", "1", "true", "right"}
_NO = {"n", "no", "wrong", "ne", "-", "0", "false", "bad"}
# "I have this song already, drop this copy." For the cases dedupe correctly
# refuses to merge on the evidence but you know are the same song anyway.
_DROP = {"drop", "dupe", "duplicate", "skip", "remove", "obrisi", "obriši"}
# "fetch this one again, the audio is bad". Written 28 times here and read as
# a bare note every one of them, because nothing claimed the word: it survived
# into the review sheet as "your note: redownload" and no stage ever acted on
# it. redownload.py selects on measured bandwidth, so a file that sounds wrong
# without measuring badly was never going to be picked up by anything else.
_REFETCH = {"redownload", "redl", "refetch", "download again", "ponovo"}
# Any http link counts as a URL hint: yt-dlp handles Audiomack, SoundCloud and
# Bandcamp as readily as YouTube, and a search-results page is answerable too.
_URL = re.compile(r"https?://\S+", re.I)
# "trim=n", "trim: no", "trim y". Answers sheet 4 and nothing else.
_TRIM_HINT = re.compile(r"^trim\s*[=:]?\s*(\S+)\s*$", re.I)
_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def _video_id(url):
    """The key hints_resolve.py filed this hint under, whatever its shape."""
    from pipeline.hints_resolve import cache_key
    return cache_key(url)


def parse_hint(hint):
    """-> ("confirm"|"reject"|"override"|"url"|None, payload).

    Confirming is by far the commonest case: the proposal is right and only
    scored low because the filename was written differently ("OK (feat. James
    Blunt)" vs the release title "OK (extended version)"). Typing one letter
    for that is the difference between an evening and a weekend.
    """
    h = (hint or "").strip()
    if not h:
        return None, None
    low = h.lower()
    if low in _YES:
        return "confirm", None
    if low in _NO:
        return "reject", None
    # "I already have this song, keep the other copy." dedupe pairs files by
    # evidence, and sometimes the evidence genuinely says no: a radio edit and
    # a video version of "Prevarena" are 48 seconds apart, which is exactly
    # the gap that stops an extended mix being swallowed by a single. Rather
    # than loosen that rule and lose real versions everywhere, this says it
    # outright for the one track.
    if low in _DROP:
        return "drop", None
    if low in _REFETCH:
        return "refetch", None
    if _URL.search(h):
        return "url", h

    # Timing, not identity. Sheet 4 asks about dead air at the head of a file
    # and about sheets written for another edit, and a question you cannot
    # answer is a question worth not asking: "trim=n" pins a file at its full
    # length, "trim=y" releases one whose silence was too long to cut blind.
    # Checked before the key=value branch below, which would otherwise read it
    # as an identity field and drop it.
    m = _TRIM_HINT.match(h)
    if m and m.group(1).lower() in _YES | _NO:
        return "trim", m.group(1).lower() in _YES

    # "artist: X; title: Y" is as natural to type as "artist=X", and a hint
    # that is ignored because of the separator is worse than no hint at all.
    if "=" in h or ":" in h:
        fields = {}
        for part in re.split(r"\s*[;|]\s*", h):
            k, sep, v = part.partition("=")
            if not sep:
                k, sep, v = part.partition(":")
            k = k.strip().lower()
            # `version` keeps a remix or bootleg distinguishable from the
            # original, which is the whole reason these rows were unmatched.
            if k in ("artist", "title", "album", "year", "version") and v.strip():
                fields[k] = v.strip()
        if fields:
            if fields.get("version") and fields.get("title"):
                fields["title"] = f"{fields['title']} ({fields.pop('version')})"
            fields.pop("version", None)
            return "override", fields
    # Bare text with no marker is a note, whatever its length.
    #
    # It used to become the title when it was short enough and read like
    # prose, which turned "same as the one above, you didn't merge" into a
    # song name. The length and prose tests were meant to catch that, and
    # they only caught the wordy half: "redownload" is one word and reads
    # like a title, so 28 tracks shipped as `Adele - redownload` at
    # confidence 1.0 without anyone being asked. Measured across every hint
    # ever given here, 31 bare hints existed and all 31 were notes. None was
    # a title.
    #
    # So there is no guess left worth making. AGENTS.md settled it already:
    # an unparseable hint is a note, not a title, and a blank field beats a
    # wrong one. Setting a title on purpose is still one keystroke away and
    # unambiguous: `title: Beograd`.
    return "note", h


# A note written next to a link, e.g. "<url> (Blackbear is the artist)".
# Pasting the link and saying who it is in the same breath is the natural
# thing to do, and throwing the note away wastes the better half of the hint.
_NOTE_ARTIST = re.compile(
    r"\(?\s*([^()]{2,60}?)\s+(?:is|are)\s+the\s+(?:real\s+)?artist\b", re.I)
_NOTE_FIELD = re.compile(
    r"\b(artist|title|album|year)\s*[:=]\s*([^;|)\n]{2,60})", re.I)


def hint_note(hint):
    """-> {field: value} written alongside a URL, or {}."""
    h = (hint or "")
    rest = _URL.sub(" ", h)
    fields = {}
    m = _NOTE_ARTIST.search(rest)
    if m:
        fields["artist"] = m.group(1).strip(" ,.-")
    for k, v in _NOTE_FIELD.findall(rest):
        fields.setdefault(k.lower(), v.strip())
    return fields


def score(entry):
    """Return (confidence 0..1, [reasons]). Reasons explain the number."""
    reasons = []
    m = entry.get("match")
    if entry.get("error"):
        return 0.0, [f"lookup error: {entry['error']}"]
    if not m:
        if entry.get("n_results"):
            return 0.0, ["fingerprint found but no usable recording metadata"]
        return 0.0, ["no acoustic fingerprint match"]

    conf = float(m.get("combined") or 0)
    a_sim = float(m.get("artist_similarity") or 0)
    t_sim = float(m.get("title_similarity") or 0)
    fp = m.get("fingerprint_score")
    from_text = fp is None

    if not entry.get("parsed_artist"):
        # Title-only filename: nothing corroborates the artist the audio claims.
        conf *= 0.85
        reasons.append("filename has no artist to corroborate")
    elif a_sim < 0.5:
        # The classic cover/live trap: audio matches, credited artist does not.
        conf *= 0.55
        reasons.append(f"artist mismatch vs filename (similarity {a_sim:.2f})")
    if t_sim < 0.6 and entry.get("parsed_title"):
        conf *= 0.7
        reasons.append(f"title differs from filename (similarity {t_sim:.2f})")
    src = m.get("source") or ""
    web_agree = src.startswith("web:") and "+" in src
    if from_text and web_agree:
        # Two independent catalogues agreed with a name a human typed into the
        # filename. The penalty below guards against a blind text search
        # landing on a same-titled different song; independent agreement on a
        # name we did not invent is the substitute for that missing audio
        # check, so it is not charged twice.
        reasons.append(f"corroborated by {src[4:].replace('+', ' and ')}")
    elif from_text:
        # No acoustic confirmation: the name matched a database, the audio was
        # never checked. Real risk of a same-titled different song.
        conf *= 0.9
        reasons.append(f"name-only match via {m.get('source', 'text search')}, no audio confirmation")
    elif fp < 0.8:
        conf *= 0.9
        reasons.append(f"weak fingerprint score ({fp:.2f})")
    if not m.get("release"):
        conf *= 0.95
        reasons.append("no album/year resolved")
    else:
        # Only real release TYPES belong here. Discogs formats (CD, Vinyl,
        # Cassette, File) used to land in this field and flagged 170 rows for
        # nothing; they are stored under `format` now and ignored.
        rtype = (m["release"] or {}).get("type")
        if rtype and rtype not in ("Album", "EP", "Single", "Compilation"):
            reasons.append(f"album is a {rtype}, may not be the original release")
    if (m.get("n_candidates") or 0) > 12:
        reasons.append(f"{m['n_candidates']} competing candidates")
    if not reasons:
        reasons.append("clean match")
    return max(0.0, min(1.0, conf)), reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto-thresh", type=float, default=0.90)
    ap.add_argument("--review-thresh", type=float, default=0.55)
    args = ap.parse_args()

    if not os.path.exists(IDENT):
        sys.exit("no cache/identity.json - run identify.py first")
    ident = json.load(open(IDENT))
    analysis = {}
    if os.path.exists(ANALYSIS):
        for v in json.load(open(ANALYSIS)).values():
            if v.get("path"):
                analysis[v["path"]] = v
    hints = load_hints()
    adopt_orphan_hints(hints, [e["name"] for e in ident.values() if e.get("name")])
    # Resolved YouTube-URL hints, produced by hints_resolve.py. Absent until
    # that has been run, in which case a URL hint just parks the row.
    yt = json.load(open(YT_HINTS)) if os.path.exists(YT_HINTS) else {}
    # Filename guesses for tracks no database could match, from
    # from_filename.py. A MusicBrainz-confirmed guess is as good as any other
    # text match; an unconfirmed one is still the best name available but is
    # kept below the auto bar so it stays visible.
    fng = json.load(open(FN_GUESS)) if os.path.exists(FN_GUESS) else {}
    override_table = bpm_overrides.load()

    # Fold in the text-search pass. Fingerprinting and text search find
    # different tracks (measured: 40 vs 56 exclusive), so the union is what
    # matters. A fingerprint match wins ties -- it identifies the actual audio,
    # where text search only agrees that a name exists somewhere.
    text = {}
    if os.path.exists(TEXTSEARCH):
        text = json.load(open(TEXTSEARCH))
    for path, t in text.items():
        best = t.get("best")
        if not best or ident.get(path, {}).get("match"):
            continue
        e = ident.setdefault(path, {"name": t["name"], "path": path})
        a, ti, _ = tagseed.seed_for(t.get("path",""), os.path.splitext(t["name"])[0])
        e["parsed_artist"], e["parsed_title"] = a, ti
        e["match"] = {
            "recording_id": best.get("recording_id"),
            "title": best.get("title"),
            "artist": best.get("artist"),
            # Text search has no acoustic evidence; record that honestly so
            # the confidence penalty for a weak fingerprint does not fire.
            "fingerprint_score": None,
            "artist_similarity": best.get("artist_similarity"),
            "title_similarity": best.get("title_similarity"),
            "combined": best.get("combined"),
            "release": best.get("release"),
            "source": best.get("source"),
        }

    for path, g in fng.items():
        if g.get("status") in (None, "unsplittable"):
            continue
        e = ident.get(path)
        if not e or e.get("match"):
            continue
        confirmed = g["status"] == "musicbrainz-confirmed"
        e["parsed_artist"] = e.get("parsed_artist") or g["artist"]
        e["parsed_title"] = e.get("parsed_title") or g["title"]
        e["match"] = {
            "recording_id": g.get("recording_id"),
            "title": g["title"], "artist": g["artist"],
            "fingerprint_score": None,
            "artist_similarity": 1.0, "title_similarity": 1.0,
            # Confirmed: trusted like any other name-only match. Unconfirmed:
            # deliberately under the auto bar, so believing a filename is
            # always a visible choice rather than a silent one.
            "combined": 0.95 if confirmed else 0.70,
            "release": g.get("release"),
            "source": "filename+musicbrainz" if confirmed else "filename-only",
        }

    # Streaming-catalogue corroboration of those filename names, from
    # webmatch.py. This only ever runs on names that are already the best
    # available, so it can raise a match but must never lower one.
    wm = json.load(open(WEBMATCH)) if os.path.exists(WEBMATCH) else {}
    for path, w in wm.items():
        e = ident.get(path)
        if not e or w["grade"] == "C":
            continue
        m = e.get("match") or {}
        if (m.get("combined") or 0) >= w["confidence"]:
            continue
        e["parsed_artist"] = e.get("parsed_artist") or w["artist"]
        e["parsed_title"] = e.get("parsed_title") or w["title"]
        rel = dict(m.get("release") or {})
        if w.get("album"):
            rel["album"] = w["album"]
        if w.get("year"):
            rel["year"] = w["year"]
        e["match"] = {
            "recording_id": m.get("recording_id"),
            "title": w["title"], "artist": w["artist"],
            "fingerprint_score": None,
            "artist_similarity": 1.0, "title_similarity": 1.0,
            # A: two independent catalogues agreed, which is as much evidence
            # as a text match ever gets. B: one did, so it clears the review
            # queue's floor but stays under the auto bar for confirmation.
            "combined": w["confidence"],
            "release": rel or None,
            "source": "web:" + "+".join(w["sources"]),
            "cover_url": w.get("cover"),
        }

    # The evidence store, if there is one. Absent is normal: nothing has run
    # `evidence.py --backfill` on a fresh clone, and scoring has to work
    # without it rather than making it a requirement.
    ev = None
    if os.path.exists(evidence.DB):
        try:
            ev = evidence.connect(evidence.DB, readonly=True)
        except Exception as exc:                          # pragma: no cover
            print(f"  evidence store unreadable, scoring without it: "
                  f"{str(exc)[:60]}")

    rows = []
    for path, e in ident.items():
        # A source file can be deleted between runs -- a duplicate you removed
        # by hand, a bad re-download withdrawn. Its identity lingers in the
        # caches, and scoring it produces a row for a file that cannot be
        # copied, which makes write_tags error and (correctly) refuse to prune.
        if not os.path.exists(path):
            continue
        conf, reasons = score(e)
        m = e.get("match") or {}
        rel = m.get("release") or {}
        a = analysis.get(path, {})
        a, _ov = bpm_overrides.apply(a, path, override_table)

        # Audio-analysis warnings are a separate axis from identification:
        # they never block a match, but they belong in front of the user.
        flags = []
        if a.get("bpm_verdict") == "octave":
            flags.append(f"BPM ambiguous ({a.get('bpm_rhythm')} vs {a.get('bpm_percival')})")
        elif a.get("bpm_verdict") == "disagree":
            flags.append("BPM engines disagree")
        if a.get("bpm_verdict") == "disagree":
            flags.append(f"BPM UNRELIABLE - engines disagree "
                         f"({a.get('bpm_rhythm')}/{a.get('bpm_degara')}/{a.get('bpm_percival')})")
        if (a.get("key_agreement") or "").startswith("1/"):
            flags.append(f"key uncertain - all 3 profiles disagreed ({a.get('camelot')})")
        if a.get("truncated"):
            flags.append(f"FILE TRUNCATED: {a.get('decoded_secs')}s of {a.get('header_secs')}s")
        if a.get("bpm") and 70 <= float(a["bpm"]) < 100 and not a.get("bpm_override"):
            flags.append(f"BPM may be half-time ({a['bpm']} or {a['bpm']*2:.0f})")
        # A tempo that contradicts the reference for the proposed match is
        # evidence against the MATCH, not the tempo, so it belongs here.
        if a.get("bpm_override") == "flag_identity":
            flags.append(f"IDENTITY SUSPECT - {a.get('bpm_override_reason')}")
            # Halve the score so it sorts to the top of the worst-first queue
            # instead of sitting in the auto-accepted tail where nobody looks.
            conf *= 0.5
            reasons = (reasons or []) + ["tempo contradicts the reference "
                                         "for this match"]
        if a.get("quality_suspect"):
            flags.append("claims high bitrate but spectrum says otherwise")
        if a.get("quality_grade") == "low":
            flags.append(f"low audio quality (cutoff {a.get('spectral_cutoff_hz')}Hz)")

        # A hint is the user's judgement and outranks every heuristic above.
        kind, payload = parse_hint(hints.get(e["name"], ""))
        if kind == "confirm" and m.get("artist"):
            conf = 1.0
            reasons = ["confirmed by you"]
            m["source"] = (m.get("source") or "") + "+confirmed"
        elif kind == "reject":
            conf = 0.0
            reasons = ["rejected by you"]
            m = {}
            e["match"] = None
        elif kind == "url":
            vid = _video_id(payload)
            res = yt.get(vid or "")
            # Anything written next to the link is your judgement and beats
            # whatever the video's own metadata said. Read before the branch,
            # not inside it: a link that names the song but not the artist is
            # answered by writing the artist beside it, and testing only
            # res["artist"] made that the one case where the note was parsed
            # and then ignored. It is also the name the identity check below
            # has to compare, since it is the one that ends up on the row.
            note = hint_note(payload)
            resolved = bool(res) and not res.get("error")
            if resolved and (res.get("artist") or
                             (res.get("title") and note.get("artist"))):
                # A link answers "which song is this", so a link that names a
                # different song invalidates what the rejected match knew.
                m, rel, _ = rename_match(m, rel,
                                         note.get("artist") or res.get("artist"),
                                         note.get("title") or res["title"],
                                         res.get("recording_id"))
                if res.get("recording_id"):
                    m["recording_id"] = res["recording_id"]
                rel = dict(res.get("release") or rel or {})
                m["source"] = "youtube-hint"
                for k in ("album", "year"):
                    if note.get(k):
                        rel[k] = note[k]
                m["release"] = rel
                e["match"] = m
                # A pasted search is a supported way to answer, but its result
                # is the first thing YouTube offered, not a video anyone
                # picked. Scoring it 1.0 put a guess through the auto gate and
                # removed the row from review, which is exactly where a guess
                # belongs. A note beside the search is still your judgement,
                # so that keeps full confidence.
                searched = bool(res.get("from_search")) and not note
                conf = SEARCH_HINT_CONF if searched else 1.0
                where = ("from a search you pasted" if searched
                         else "from your link")
                reasons = [f"{where} ({res.get('derived_from')}"
                           f"{', your note' if note else ''}"
                           f"{', confirmed on MusicBrainz' if res.get('enriched') else ''})"]
            elif resolved and res.get("title"):
                # Resolved, it named no artist, and you have not supplied one
                # either. hints_resolve declined to invent one, so the row
                # stays in the queue saying what it does know. Saying "not
                # resolved yet" here would send you off to re-run a stage that
                # has already done its job and would decline again.
                reasons = (reasons or []) + [
                    f"your link says the song is \"{res['title']}\" but names "
                    f"no artist - add one as 'artist: X' beside the link"]
            else:
                reasons = (reasons or []) + [
                    "YouTube link not resolved yet - run hints_resolve.py"]
        elif kind == "note":
            # Not actionable automatically, but it is something you told me,
            # so it stays visible instead of vanishing.
            reasons = (reasons or []) + [f"your note: {payload[:60]}"]
        elif kind == "override":
            # The same boundary as the link branch. A name you typed replaces
            # the match just as completely as a link does, so the album, year,
            # recording ID and artwork the old match brought with it are no
            # more true here than they were there.
            m, rel, _ = rename_match(
                m, rel,
                payload.get("artist") or (m or {}).get("artist"),
                payload.get("title") or (m or {}).get("title"))
            if payload.get("album"):
                rel["album"] = payload["album"]
            if payload.get("year"):
                rel["year"] = payload["year"]
            m["release"] = rel
            e["match"] = m
            conf = 1.0
            reasons = ["set by you"]

        # What every other source said about this name, counted in independent
        # families. Applied after the hint block on purpose: an answer you gave
        # is not something a catalogue gets to argue with.
        if ev is not None and conf < 1.0:
            mult, why = confidence.penalty(
                ev, path, {"artist": m.get("artist"), "title": m.get("title")})
            if why:
                conf *= mult
                reasons = (reasons or []) + why

        tier = ("unmatched" if kind == "reject" else
                "auto" if (kind in ("confirm", "override")
                           or (kind == "url" and conf >= 1.0)) else
                "hinted" if e["name"] in hints else
                "unmatched" if not m else
                "auto" if conf >= args.auto_thresh else
                "review" if conf >= args.review_thresh else
                "suspect")
        rows.append({
            "file": e["name"],
            "path": path,
            "confidence": round(conf, 3),
            "tier": tier,
            "reasons": reasons,
            "flags": flags,
            "balkan": likely_balkan(e["name"]),
            "proposed_artist": m.get("artist"),
            "proposed_title": m.get("title"),
            "proposed_album": rel.get("album"),
            "proposed_year": rel.get("year"),
            # Where the identity came from. write_tags stamps it as
            # MUZZI_SOURCE; without it here that stamp fell back to the
            # literal "acoustid" on every file, which made the value a lie and
            # left the "Needs identification" playlist permanently empty
            # because nothing could ever be "audio-only".
            "source": m.get("source"),
            "recording_id": m.get("recording_id"),
            "release_group_id": rel.get("release_group_id"),
            "bpm": a.get("bpm"),
            "key": a.get("camelot"),
            "quality": a.get("quality_grade"),
            "hint": hints.get(e["name"], ""),
            "songkey": _songkey(m.get("artist"), m.get("title"), e["name"]),
            "filekey": _filekey(e["name"]),
        })

    # One hint should settle a song, not one file. The same track arrives as
    # both .mp3 and .m4a from the two YouTube folders, so a hint typed against
    # either copy is applied to all of them.
    # Propagate on the FILENAME only. songkey is derived from the proposal,
    # which a hint rewrites -- so keying on it let a YouTube link typed against
    # "Crystal Rock - Read All About It" jump onto an unrelated "The Night We
    # Met.mp3" once the link resolved to that name. A filename never moves.
    by_file = {}
    for r in rows:
        if r["hint"]:
            by_file.setdefault(r["filekey"], r["hint"])
    for r in rows:
        if not r["hint"]:
            r["hint"] = by_file.get(r["filekey"], "")

    rows.sort(key=lambda r: (r["tier"] == "auto", r["confidence"], r["file"]))
    json.dump(rows, open(OUT, "w"), ensure_ascii=False, indent=1)

    # Two queues, because they are two different jobs. Verifying a proposed
    # match is a two-second yes/no; identifying an unmatched track means going
    # and finding out what it is. Mixing them makes the fast work feel slow.
    # Include auto-accepted rows too, still worst-first. The queue is meant to
    # be worked top-down for as long as there is time; truncating it at the
    # auto threshold would cap how deep you can go.
    # Never queue a file that will not be written. dedupe drops the worse copy
    # of each duplicate pair, so reviewing those is pure wasted attention:
    # 268 of 1698 rows.
    losers = set()
    if os.path.exists(DUPES):
        for g in json.load(open(DUPES)):
            losers.update(g.get("drop") or [])

    def collapse(items):
        """One row per song. The same track appears as .mp3 and .m4a from the
        two YouTube folders; deciding it twice is the same decision twice."""
        out, seen = [], {}
        for r in items:
            if r["path"] in losers:
                continue
            k = r["filekey"] or _norm(r["file"])
            if k in seen:
                seen[k]["copies"] += 1
                continue
            r = dict(r, copies=1)
            seen[k] = r
            out.append(r)
        return out

    verify = collapse([r for r in rows
                       if r["tier"] in ("review", "suspect", "auto")])
    unknown = collapse([r for r in rows if r["tier"] == "unmatched"])

    cols = ["rank", "confidence", "tier", "copies", "file", "proposed_artist",
            "proposed_title", "proposed_album", "proposed_year", "why",
            "audio_flags", "hint"]

    def write(path, items, with_proposal=True):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, delimiter="\t",
                               quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
            w.writeheader()
            for i, r in enumerate(items, 1):
                w.writerow({
                    "rank": i,
                    "confidence": r["confidence"],
                    "tier": r["tier"],
                    "copies": r.get("copies", 1),
                    "file": r["file"],
                    "proposed_artist": (r["proposed_artist"] or "") if with_proposal else "",
                    "proposed_title": (r["proposed_title"] or "") if with_proposal else "",
                    "proposed_album": (r["proposed_album"] or "") if with_proposal else "",
                    "proposed_year": (r["proposed_year"] or "") if with_proposal else "",
                    "why": "; ".join(r["reasons"]),
                    "audio_flags": "; ".join(r["flags"]),
                    "hint": r["hint"],
                })

    # Least confident first: an hour at the top of this list fixes more than
    # an hour anywhere else in it.
    write(QUEUE, verify)
    write(UNKNOWN_QUEUE, unknown, with_proposal=False)
    publish_sheets(sheet_groups(rows), hints)

    from collections import Counter
    tiers = Counter(r["tier"] for r in rows)
    n = len(rows)
    print(f"\n  {n} tracks scored\n")
    for t in ("auto", "review", "suspect", "unmatched", "hinted"):
        if tiers.get(t):
            print(f"    {t:10} {tiers[t]:4}  ({100*tiers[t]/n:.0f}%)")
    bal = [r for r in rows if r["balkan"]]
    if bal:
        auto_b = sum(1 for r in bal if r["tier"] == "auto")
        print(f"\n    of {len(bal)} likely-Balkan tracks, {auto_b} auto-accepted")
    n_below = sum(1 for r in verify if r["tier"] != "auto")
    print(f"\n  verify queue  ({len(verify)} rows, worst first; "
          f"first {n_below} are below the auto bar) -> {QUEUE}")
    print(f"  unknown queue ({len(unknown)} rows, need a hint)  -> {UNKNOWN_QUEUE}")
    print(f"  full detail -> {OUT}\n")
    print("  Top 10 proposed matches most likely to be WRONG:")
    for r in verify[:10]:
        print(f"    {r['confidence']:.2f}  {r['file'][:44]:44} "
              f"-> {str(r['proposed_artist'])[:18]:18} | {'; '.join(r['reasons'])[:46]}")
    print()


if __name__ == "__main__":
    main()
