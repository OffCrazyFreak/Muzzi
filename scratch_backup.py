"""Apply the answers already in hints.tsv, against the real library.

Run from the main checkout deliberately: that is where the music, the caches
and hints.tsv are, and the owner asked for this. Backups were taken first.

yt_links writes only its own sheet, so review sheets 1 to 4 are untouched.
"""
import subprocess
import sys

MAIN = "/home/silver/Desktop/Muzzi"
PY = f"{MAIN}/.venv/bin/python"

step = sys.argv[1]
CMDS = {
    "links": [PY, f"{MAIN}/pipeline/yt_links.py", "--apply"],
    "hints": [PY, f"{MAIN}/pipeline/hints_resolve.py"],
    "refetch-dry": [PY, f"{MAIN}/pipeline/redownload.py", "--requested",
                    "--dry-run"],
    "refetch": [PY, f"{MAIN}/pipeline/redownload.py", "--requested",
                "--batch", "10"],
}
p = subprocess.run(CMDS[step], cwd=MAIN, capture_output=True, text=True)
print(p.stdout[-6000:])
if p.returncode:
    print("STDERR:", p.stderr[-3000:])
print("rc", p.returncode)
