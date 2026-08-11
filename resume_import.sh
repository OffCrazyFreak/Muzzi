#!/usr/bin/env bash
# Resume the import over cache/remaining.txt, detached and unbuffered so
# progress is visible and a session teardown cannot kill it.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
mapfile -t FILES < cache/remaining.txt
printf 'resuming %d files at %s\n' "${#FILES[@]}" "$(date +%H:%M:%S)"
./beet import -q -s "${FILES[@]}"
printf 'done at %s\n' "$(date +%H:%M:%S)"
