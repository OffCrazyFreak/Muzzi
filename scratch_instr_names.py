#!/usr/bin/env python3
"""Scratch: if the instrumental flag were loosened, what would catch the NCS case?

The flag exists because an instrumental with under three transcribed words was
still offered 4000 characters of someone else's lyrics. Loosening it is only
safe if the artist/title comparison catches those on its own. So: for every
track marked instrumental that has lyrics, how well does the sheet's name
match the track's?
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pipeline.webmatch import MIN_FIT, fit  # noqa: E402

rows = {r["path"]: r for r in
        json.load(open(os.path.join(HERE, "cache", "review.json")))}
lv = json.load(open(os.path.join(HERE, "cache", "lyric_verify.json")))
lyr = json.load(open(os.path.join(HERE, "cache", "lyrics.json")))

buckets = collections.Counter()
examples = []
for p, v in lv.items():
    if not isinstance(v, dict) or not v.get("instrumental"):
        continue
    r = rows.get(p) or {}
    k = f"{r.get('proposed_artist') or ''}|{r.get('proposed_title') or ''}"
    e = lyr.get(k.lower())
    if not isinstance(e, dict) or not (e.get("plain") or e.get("synced")):
        continue
    matched = e.get("matched") or ""
    ma, _, mt = matched.partition(" - ")
    af = e.get("artist_fit")
    tf = e.get("title_fit")
    if af is None and r.get("proposed_artist"):
        af = fit(r["proposed_artist"], ma)
    if tf is None and r.get("proposed_title"):
        tf = fit(r["proposed_title"], mt)
    worst = min([x for x in (af, tf) if x is not None] or [None]) \
        if (af is not None or tf is not None) else None
    bal = "balkan" if r.get("balkan") else "other"
    if worst is None:
        buckets[f"{bal} no recorded match"] += 1
    elif worst >= MIN_FIT:
        buckets[f"{bal} name MATCHES, only the flag refuses it"] += 1
        if len(examples) < 12:
            examples.append((bal, round(worst, 2), r.get("file", "")[:38],
                             matched[:34]))
    else:
        buckets[f"{bal} name disagrees, caught anyway"] += 1

print(f"  {sum(buckets.values())} tracks marked instrumental with lyrics\n")
for k in sorted(buckets):
    print(f"    {k:48s} {buckets[k]}")
print("\n  where only the flag refuses it:")
for bal, w, f, m in examples:
    print(f"    {bal:7s} fit {w}  {f:40s} <- {m}")
