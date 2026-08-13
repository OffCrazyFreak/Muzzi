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


def walk(roots, exts=AUDIO):
    """-> every audio file under roots, each real directory visited once.

    Takes all the roots at once rather than one at a time, because "visited
    once" has to hold across them. Two entries in input/ can be links to the
    same folder, or one root can sit inside another; walking them separately
    means a private `seen` per walk, and the library is processed twice with
    nothing reporting it. A single string is accepted for one root.
    """
    if isinstance(roots, str):
        roots = [roots]
    seen = set()
    for root in roots:
        for dirpath, dirs, names in os.walk(root, followlinks=True):
            try:
                st = os.stat(dirpath)
            except OSError:
                dirs[:] = []              # vanished or unreadable mid-walk
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                dirs[:] = []
                continue
            seen.add(key)
            # The canonical location, not the route taken to it. Reaching one
            # folder through a link inside an earlier root would otherwise
            # record every file under that link, so a path-keyed cache went
            # stale the moment the link was renamed, analyze filed a second
            # entry for one physical file, and write_tags mirrored the link's
            # subfolder layout into the output.
            real = os.path.realpath(dirpath)
            for n in sorted(names):
                if n.lower().endswith(exts):
                    yield os.path.join(real, n)
