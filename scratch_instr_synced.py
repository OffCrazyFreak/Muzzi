#!/usr/bin/env python3
"""Scratch: is a timed sheet a safe discriminator for "this song has vocals"?

Nobody writes a 40-line timed LRC for an instrumental. The NCS failure was a
wrong match handing over PLAIN text, so if the flagged tracks mostly hold long
SYNCED sheets, that is independent evidence they have words.
"""
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
rows = {r["path"]: r for r in
        json.load(open(os.path.join(HERE, "cache", "review.json")))}
lv = json.load(open(os.path.join(HERE, "cache", "lyric_verify.json")))
lyr = json.load(open(os.path.join(HERE, "cache", "lyrics.json")))

c = collections.Counter()
ex = []
for p, v in lv.items():
    if not isinstance(v, dict) or not v.get("instrumental"):
        continue
    r = rows.get(p) or {}
    k = f"{r.get('proposed_artist') or ''}|{r.get('proposed_title') or ''}"
    e = lyr.get(k.lower())
    if not isinstance(e, dict):
        continue
    syn, pl = e.get("synced"), e.get("plain")
    if not (syn or pl):
        continue
    lines = len([ln for ln in (syn or "").splitlines() if ln.strip()])
    kind = (f"synced, {'8+' if lines >= 8 else 'under 8'} lines" if syn
            else "plain only")
    c[kind] += 1
    if lines >= 8 and len(ex) < 8:
        ex.append((lines, r.get("file", "")[:42]))

for k in sorted(c):
    print(f"  {k:26s} {c[k]}")
print()
for n, f in ex:
    print(f"    {n:3d} timed lines  {f}")
