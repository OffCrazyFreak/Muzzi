# Contributing

Contributions are welcome. This is a one-person project, so a short issue
before a large PR saves us both the wasted work.

## Setup

Python 3.11 to 3.14. The pins were verified on 3.12.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp config/secrets.example.json config/secrets.json
cp config/config.example.yaml config/config.yaml
```

Fill both in (all keys are free, see the README), install `ffmpeg`, `yt-dlp`
and `bin/fpcalc`, then run against a copy of a few files:

```bash
./.venv/bin/python run.py "/path/to/a/few/songs" --dry-run
```

## What is most useful

- **Coverage for music the big databases miss.** Ex-Yugoslav catalogue is the
  reason this exists, but the same gap exists for other regions. New sources,
  better matching, entries for `config/scenes.json`.
- **Player compatibility.** A tag that a real Android player reads differently
  than expected, with the player named.
- **Correctness in the pipeline.** Every stage is idempotent and cache-backed;
  bugs usually show up as a stage that redoes work or writes something twice.

## Rules

- **Read `AGENTS.md` first.** It holds the conventions, including the target
  players. Every metadata change must say what Namida does with it, what
  Samsung Music does with it, and what a generic Android player does with it.
- **Never commit personal data.** `hints.tsv`, `review/`, `cache/`, `out/`,
  `config/secrets.json`, `config/config.yaml` and any audio are gitignored for
  a reason. Check `git status` before committing.
- **Source folders are read-only.** Nothing may write to the input.
- **CI runs `ruff check .`.** Run it locally first. The rule set in `ruff.toml`
  is narrow on purpose: real defects, not style.

## Commits and PRs

Conventional commits, scoped to the pipeline stage:

```text
type(scope): Short summary in imperative mood

Changes:
- Specific change

Brief explanation of why the change was needed.
```

The why is the part worth reading in a year, and this repository's history is
full of decisions that look arbitrary without it. One PR does one thing. See
`AGENTS.md` for the full list of types and scopes.

No em dashes or en dashes in Markdown. CI fails on them.
