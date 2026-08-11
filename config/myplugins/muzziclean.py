"""muzziclean - strip YouTube/rip cruft from filename-derived tags before matching.

Why this exists: `fromfilename` seeds artist/title from the filename but does no
cleanup, so beets tries to match a title like "High Hopes (Official Video)" and
the decoration inflates the distance past the accept threshold. Measured on this
library, stripping cruft moved Panic! At The Disco from "no match" to 0.111.

Runs on import_task_start, after fromfilename (ordering comes from plugin load
order, so keep `tempoclean` listed after `fromfilename` in config).

Only in-memory tags are touched. Files on disk are never renamed.
"""

import re

from beets.plugins import BeetsPlugin

# Bracketed decorations: (Official Video), [HD], (Lyrics), (Remaster 2011)...
_BRACKETED = re.compile(
    r"""\s*[\(\[\{]\s*
        (?:[^\)\]\}]*\b(?:
            official|lyrics?|lyric|video|audio|visualiz(?:er|ation)|
            hd|hq|4k|8k|full\s*hd|mv|clip|
            remaster(?:ed)?|explicit|clean|uncensored|
            colou?r\s*coded|
            tekst|uzivo|u[zž]ivo|spot|prevod|prijevod|domaci|doma[cć]i
        )\b[^\)\]\}]*)
    [\)\]\}]""",
    re.IGNORECASE | re.VERBOSE,
)

# Bare (unbracketed) trailing decorations.
_BARE = re.compile(
    r"""(?:
        \b(?:official\s+(?:music\s+)?(?:video|audio)|lyrics?\s+video|music\s+video)\b
      | \b(?:tekst|uzivo|u[zž]ivo|prevod|prijevod)\s*(?:/|⧸)?\s*(?:lyrics?)?\b
      | (?<=\s)(?:hd|hq|4k)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# yt-dlp replaces filesystem-illegal characters with fullwidth lookalikes.
_FULLWIDTH = str.maketrans({
    "？": "?", "＂": '"', "＊": "*", "：": ":",
    "＜": "<", "＞": ">", "｜": "|", "／": "/", "⧸": "/", "＼": "\\",
})

_LEFTOVER = re.compile(r"\s*[\(\[\{]\s*[\)\]\}]")   # emptied brackets
_DASHES = re.compile(r"[\s\-–—_]+$")
_SPACES = re.compile(r"\s{2,}")


def clean_text(value):
    """Strip rip decorations. Returns the cleaned string (possibly unchanged)."""
    if not value:
        return value
    out = value.translate(_FULLWIDTH)
    prev = None
    while out != prev:                       # decorations often stack
        prev = out
        out = _BRACKETED.sub(" ", out)
        out = _BARE.sub(" ", out)
    out = _LEFTOVER.sub("", out)
    out = _SPACES.sub(" ", out).strip()
    out = _DASHES.sub("", out).strip()
    # Never hand back an empty title just because it was all decoration.
    return out or value


class MuzziCleanPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add({"auto": True, "fields": ["title", "artist", "album"]})
        if self.config["auto"]:
            self.register_listener("import_task_start", self.on_task_start)

    def on_task_start(self, task, session):
        for item in getattr(task, "items", None) or [getattr(task, "item", None)]:
            if item is None:
                continue
            for field in self.config["fields"].as_str_seq():
                old = getattr(item, field, None)
                new = clean_text(old)
                if new and new != old:
                    self._log.debug("{}: {!r} -> {!r}", field, old, new)
                    setattr(item, field, new)
