#!/usr/bin/env python3
"""Walking the source folders, once each, symlinks and all.

`input/` is meant to hold symlinks: the library lives on another partition and
copying it to run this would be absurd. That forces `os.walk(followlinks=True)`,
because os.walk skips a symlinked directory by default and would report an
empty library rather than an error.

Following links brings back the two problems the default exists to avoid, and
both are silent rather than loud:

  * a cycle. `ln -s . loop` inside a source folder makes os.walk descend
    forever, and the run never ends rather than failing.
  * two routes to one folder. Link the same album in twice, deliberately or
    by accident, and every file in it is fingerprinted, analysed and written
    twice, which reads as duplicates the library does not have.

Both are the same fact underneath: a directory can be arrived at more than
once. So identity is the device inode and number, not the path, and a
directory already visited is not descended into again.
"""
import os

AUDIO = (".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav")


def walk(root, exts=AUDIO):
    """-> every audio file under root, each real directory visited once."""
    seen = set()
    for dirpath, dirs, names in os.walk(root, followlinks=True):
        try:
            st = os.stat(dirpath)
        except OSError:
            dirs[:] = []                  # vanished or unreadable mid-walk
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:
            dirs[:] = []
            continue
        seen.add(key)
        for n in sorted(names):
            if n.lower().endswith(exts):
                yield os.path.join(dirpath, n)
