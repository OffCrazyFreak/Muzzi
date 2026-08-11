#!/usr/bin/env python3
"""The User-Agent every outbound request identifies itself with.

MusicBrainz *requires* a descriptive agent carrying a way to reach whoever is
making the requests -- generic agents get throttled harder or blocked outright,
and the same courtesy applies to Cover Art Archive, LRCLIB and Deezer.

That contact detail is personal, so it is read from config/secrets.json rather
than written into the source. The project URL is the fallback, which
MusicBrainz accepts in place of an address and which means a fresh clone works
without configuring anything at all.
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = os.path.join(HERE, "config", "secrets.json")

PROJECT_URL = "https://github.com/OffCrazyFreak/Muzzi"
VERSION = "0.1"


def contact():
    """-> the contact string for the User-Agent, never raising.

    A missing or malformed secrets.json is not an error worth stopping for:
    every caller here is doing network I/O that works fine with the URL.
    """
    try:
        with open(SECRETS, encoding="utf-8") as fh:
            c = (json.load(fh).get("contact") or "").strip()
        return c or PROJECT_URL
    except Exception:
        return PROJECT_URL


UA = {"User-Agent": f"Muzzi/{VERSION} ( {contact()} )"}
