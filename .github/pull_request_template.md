<!--
Small, focused PRs are far more likely to be merged. One concern per PR:
if the description says "also", split it.
-->

## What changed

<!-- Keep the scope tight. -->

## Why

<!-- The problem being solved, and why this approach. -->

## Player support

<!-- Only if this changes a tag, a filename or an output file.
     What does Namida do with it, what does Samsung Music do with it, and what
     does a generic Android player do with it? Delete if not applicable. -->

## Checklist

- [ ] `ruff check .` passes
- [ ] I said which commands I ran and which I did not
- [ ] No personal data: no audio, artwork, lyrics, `hints.tsv`, `review/`,
      `cache/`, `out/`, or filled-in config
- [ ] Source folders were not written to
- [ ] Every surface covered: both containers, both lyric carriers, both BPM
      carriers, both playlist forms
- [ ] README updated in this PR if behaviour changed
