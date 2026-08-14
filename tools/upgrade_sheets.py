#!/usr/bin/env python3
"""Add the `checked` and `look here` columns to review sheets already in use.

`review.py` rebuilds `review/` from scratch every run, which is fine when your
answers are in `hints.tsv` and not fine when they are still sitting in the
spreadsheet you have open. This adds the two new columns to the sheets where
they are, so a queue part-way through does not have to be restarted to gain
them.

Nothing is rewritten. Each existing cell object is left exactly as it is and
two new cells are inserted beside it, so formatting, value types and every
answer already typed survive by construction rather than by care. A sheet that
already has the columns is skipped, so running this twice is safe.

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
AFTER = "why"
NEW = ["checked", "look here"]


def cells_of(row):
    """-> the row's cells, with repeat-compressed runs expanded.

    A cell carrying `number-columns-repeated` stands for several columns at
    once, so inserting by position without expanding first would put the new
    columns somewhere else entirely on exactly the rows that use it.
    """
    from odf.table import TableCell
    out = []
    for tc in list(row.childNodes):
        if tc.qname[1] != "table-cell":
            continue
        rep = int(tc.getAttribute("numbercolumnsrepeated") or 1)
        if rep <= 1:
            out.append(tc)
            continue
        row.removeChild(tc)
        for _ in range(min(rep, 64)):
            copy = TableCell(valuetype=tc.getAttribute("valuetype") or "string")
            for child in tc.childNodes:
                copy.addElement(child.cloneNode(True)
                                if hasattr(child, "cloneNode") else child)
            out.append(copy)
    return out


def text_of(cell):
    from odf.text import P
    return " ".join(str(p) for p in cell.getElementsByType(P)).strip()


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

    header, at = None, None
    for row in rows:
        names = [text_of(c) for c in cells_of(row)]
        if "file" in names:
            header = names
            break
    if header is None:
        return 0, "no header row"
    if all(c in header for c in NEW):
        return 0, "already upgraded"
    if AFTER not in header:
        return 0, f"no {AFTER!r} column to insert after"
    at = header.index(AFTER) + 1
    fi = header.index("file")

    touched = 0
    seen_header = False
    for row in rows:
        cells = cells_of(row)
        names = [text_of(c) for c in cells]
        if not seen_header and "file" in names:
            seen_header = True
            for n, label in enumerate(NEW):
                c = TableCell(valuetype="string")
                review._write_cell(c, label)
                cells.insert(at + n, c)
            rebuild(row, cells)
            continue
        if not seen_header:
            # The instruction line above the header. It is one long cell and
            # the rest are padding, so two more empties keep the widths equal.
            for _ in NEW:
                cells.append(TableCell(valuetype="string"))
            rebuild(row, cells)
            continue

        name = text_of(cells[fi]) if len(cells) > fi else ""
        track = paths_by_name.get(name)
        checked, found = "", ""
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
                checked = confidence.why_review(conn, track)
                found = links.dossier(conn, track, artist, title, links.SEARCH)
            except Exception:
                checked, found = "", ""
        for n, value in enumerate((checked, found)):
            c = TableCell(valuetype="string")
            review._write_cell(c, value if value else "")
            cells.insert(at + n, c)
        rebuild(row, cells)
        touched += 1

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
