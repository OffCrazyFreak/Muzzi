#!/usr/bin/env python3
"""Add the evidence columns to review sheets already in use.

`review.py` rebuilds `review/` from scratch every run, which is fine when your
answers are in `hints.tsv` and not fine when they are still sitting in the
spreadsheet you have open. This adds whichever of the new columns a sheet is
missing, where they are, so a queue part-way through does not have to be
restarted to gain them.

Nothing is rewritten. Each existing cell object is left exactly as it is and
the new cells are inserted beside it, so formatting, value types and every
answer already typed survive by construction rather than by care. A sheet that
already has the columns is skipped, so running this twice is safe, and a sheet
that has some of them gains only the rest.

Usage:
  upgrade_sheets.py --dir /path/to/review --dry-run
  upgrade_sheets.py --dir /path/to/review
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import confidence, evidence, links, review  # noqa: E402

# Where the new columns go: after `why`, which is the other column explaining
# the row, and before `audio_flags`. Matches the layout review.py now writes,
# so an upgraded sheet and a freshly built one are the same shape.
#
# In order, because they are added over time and a sheet upgraded once already
# holds the earlier ones. `on the file` arrived after the other two, so the
# common case now is a sheet that needs exactly one column inserted in the
# middle of a block it already has.
AFTER = "why"
BLOCK = ["checked", "on the file", "look here"]

# How far into a row the header search expands a repeat run. The header is a
# handful of columns; the padding after it can be thousands, and the search
# only has to reach `file`.
HEADER_SCAN = 64


def cells_of(row, upto=None):
    """-> the row's cells, with repeat-compressed runs expanded.

    A cell carrying `number-columns-repeated` stands for several columns at
    once, so inserting by position without expanding first would put the new
    columns somewhere else entirely on exactly the rows that use it.

    Expanded only as far as `upto`, which is the last index anything will be
    inserted at. Past that the run is kept compressed, with its count reduced
    by however much was expanded off the front, so the row still describes the
    same columns. A spreadsheet pads its rows to the sheet width, and that
    padding is routinely 1024 or 16384 columns: expanding it in full would
    build a million elements to insert three, and the previous `min(rep, 64)`
    avoided that by silently dropping the rest of the run, which is a rewrite
    of the document rather than a reading of it.

    Each expanded cell is a full clone. Building a fresh `TableCell` with only
    `valuetype` was losing every other attribute, and these sheets carry
    `value` on 34 of their cells, so a repeated numeric cell would have kept
    its type and lost its number.
    """
    out = []
    for tc in list(row.childNodes):
        if tc.qname[1] != "table-cell":
            continue
        rep = int(tc.getAttribute("numbercolumnsrepeated") or 1)
        if rep <= 1:
            out.append(tc)
            continue
        # How many of this run have to become real cells for the inserts to
        # land where they are meant to. The rest stays as one cell.
        need = rep if upto is None else max(0, min(rep, upto - len(out)))
        row.removeChild(tc)
        for _ in range(need):
            out.append(_clone_cell(tc))
        if rep > need:
            rest = _clone_cell(tc)
            rest.setAttribute("numbercolumnsrepeated", str(rep - need))
            out.append(rest)
        continue
    return out


def _clone_cell(tc):
    """-> a copy of one cell carrying every attribute except the repeat count.

    The repeat is dropped because the copy stands for exactly one column; the
    caller puts it back on the one cell that still stands for several.

    `copy.deepcopy`, and not the `cloneNode` this used to reach for. odfpy
    elements have no `cloneNode`, so that branch never ran and the fallback
    beside it handed the ORIGINAL child to `addElement`, which re-parents
    rather than copies. Expanding a run of five therefore moved the content
    into the first copy and left the other four blank, and emptied the cell it
    was copying from on the way. deepcopy leaves the original alone.
    """
    import copy as _copy
    out = _copy.deepcopy(tc)
    for (ns, name) in list((out.attributes or {})):
        if name == "number-columns-repeated":
            del out.attributes[(ns, name)]
    return out


def text_of(cell):
    from odf.text import P
    return " ".join(str(p) for p in cell.getElementsByType(P)).strip()


def plan_for(header):
    """-> [(index in this header, column name)] to insert, ascending.

    Each missing column goes after whichever of its predecessors the sheet
    already has, and after `why` when it has none of them. Appending them all
    at the end instead would put `on the file` beyond `look here` on a sheet
    upgraded before it existed, which is a different layout from the one
    review.py writes, and the two have to match or the next rebuild moves
    every answer a column sideways.
    """
    out = []
    for i, name in enumerate(BLOCK):
        if name in header:
            continue
        anchor = None
        for prev in reversed(BLOCK[:i]):
            if prev in header:
                anchor = header.index(prev) + 1
                break
        out.append((anchor if anchor is not None else header.index(AFTER) + 1,
                    name))
    # Stable, so two columns sharing an anchor keep the order BLOCK gives them.
    out.sort(key=lambda x: x[0])
    return out


def insert_all(cells, plan, value_of):
    """Insert the planned columns, allowing for the ones already inserted."""
    from odf.table import TableCell
    for offset, (at, name) in enumerate(plan):
        c = TableCell(valuetype="string")
        review._write_cell(c, value_of(name) or "")
        cells.insert(at + offset, c)


def rebuild(row, cells):
    for tc in list(row.childNodes):
        row.removeChild(tc)
    for tc in cells:
        row.addElement(tc)


def upgrade(path, conn, paths_by_name, dry=False):
    """-> (rows touched, message)."""
    from odf.opendocument import load
    from odf.table import Table, TableCell, TableRow

    doc = load(path)
    tables = doc.getElementsByType(Table)
    if not tables:
        return 0, "no table"
    tbl = tables[0]
    rows = tbl.getElementsByType(TableRow)

    header = None
    for row in rows:
        names = [text_of(c) for c in cells_of(row, upto=HEADER_SCAN)]
        if "file" in names:
            header = names
            break
    if header is None:
        return 0, "no header row"
    if all(c in header for c in BLOCK):
        return 0, "already upgraded"
    if AFTER not in header:
        return 0, f"no {AFTER!r} column to insert after"
    plan = plan_for(header)
    fi = header.index("file")
    # The furthest any insert reaches, so a run of padding past it is
    # never expanded.
    upto = max([at for at, _ in plan] + [len(header)]) + len(plan) + 1

    touched = 0
    seen_header = False
    failed = []
    for row in rows:
        cells = cells_of(row, upto=upto)
        names = [text_of(c) for c in cells]
        if not seen_header and "file" in names:
            seen_header = True
            insert_all(cells, plan, lambda name: name)
            rebuild(row, cells)
            continue
        if not seen_header:
            # The instruction line above the header. It is one long cell and
            # the rest are padding, so more empties keep the widths equal.
            for _ in plan:
                cells.append(TableCell(valuetype="string"))
            rebuild(row, cells)
            continue

        name = text_of(cells[fi]) if len(cells) > fi else ""
        track = paths_by_name.get(name)
        values = {}
        if track and conn is not None:
            try:
                r = {"file": name, "proposed_artist": None,
                     "proposed_title": None}
                # The names as the sheet already shows them, so the search
                # links ask for what you are looking at.
                for key, col in (("proposed_artist", "proposed_artist"),
                                 ("proposed_title", "proposed_title")):
                    if col in header and len(cells) > header.index(col):
                        r[key] = text_of(cells[header.index(col)]) or None
                artist, title = review._search_terms(r)
                values = {
                    "checked": confidence.why_review(conn, track),
                    "on the file": confidence.local_claims(conn, track),
                    "look here": links.dossier(conn, track, artist, title,
                                               links.SEARCH),
                }
            except Exception as e:
                # A lookup that threw is not a row with no evidence, and
                # writing it as empty cells says it is. The header would
                # then be present, the next run would answer "already
                # upgraded", and the blanks would be permanent. Nothing
                # is saved unless every row was answered.
                failed.append((name, f"{type(e).__name__}: {str(e)[:60]}"))
        insert_all(cells, plan, values.get)
        rebuild(row, cells)
        touched += 1

    if failed:
        # Refused whole rather than half applied. This tool exists so a
        # part-answered queue keeps its answers, and a sheet that gained
        # the columns but not their contents can never gain them later.
        first = "; ".join(f"{n}: {w}" for n, w in failed[:3])
        return 0, f"{len(failed)} rows could not be looked up, "\
                  f"nothing written ({first})"
    if dry:
        return touched, "would upgrade"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, f"{path}.bak-{stamp}")
    doc.save(path)
    return touched, "upgraded"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=review.REVIEW_DIR,
                    help="the review folder to upgrade")
    ap.add_argument("--db", default=evidence.DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        sys.exit(f"no such folder: {args.dir}")

    conn = None
    if os.path.exists(args.db):
        try:
            conn = evidence.connect(args.db, readonly=True)
        except Exception as e:
            print(f"  no evidence store ({e}); columns will be added empty")
    else:
        print("  no evidence store; columns will be added empty")

    # The sheets hold basenames; the store holds full paths.
    paths_by_name = {}
    if conn is not None:
        for (p,) in conn.execute(
                "SELECT DISTINCT track_path FROM observation").fetchall():
            paths_by_name.setdefault(os.path.basename(p), p)

    print(f"\n  {args.dir}")
    for name in sorted(os.listdir(args.dir)):
        if not name.lower().endswith(".ods"):
            continue
        n, msg = upgrade(os.path.join(args.dir, name), conn, paths_by_name,
                         dry=args.dry_run)
        print(f"    {name:38} {msg}, {n} rows")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
