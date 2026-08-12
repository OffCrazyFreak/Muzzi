# Security

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting][report]. Do not open a public
issue for anything exploitable. Expect a first reply within a week.

[report]: https://github.com/OffCrazyFreak/Muzzi/security/advisories/new

## Scope

Muzzi runs locally and listens on nothing. It does make outbound requests, to
AcoustID, MusicBrainz, Discogs, Last.fm, Genius, Deezer, iTunes, LRCLIB, Cover
Art Archive and YouTube. So the interesting surface is the credentials those
need, and the files Muzzi writes:

- **API keys.** `config/secrets.json` and `config/config.yaml` hold AcoustID,
  Discogs, Last.fm and Genius tokens. Both are gitignored; the committed
  `*.example.*` files show the shape and hold nothing real. A key leaked
  through a log line, an error message or a User-Agent is a valid report.
- **Data written outside the output folder.** Source audio is read-only by
  design. Anything that writes to a source folder is a valid report.
- **Untrusted input.** Filenames, tags and API responses all reach the shell,
  the filesystem and the tag writer. Path traversal or command injection from
  any of them is a valid report.

Out of scope: rate limits or bans from an upstream service, and anything
requiring an attacker who already has your shell.

## Supported versions

There are no releases yet. Only `main` is supported.
