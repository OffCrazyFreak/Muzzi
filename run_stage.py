"""Drive a stage against the real library. Delete when the run is done."""
import subprocess
import sys

MAIN = "/home/silver/Desktop/Muzzi"
PY = f"{MAIN}/.venv/bin/python"
CMDS = {
    "intros": [PY, f"{MAIN}/pipeline/intros.py"],
    "review": [PY, f"{MAIN}/pipeline/review.py"],
    "links": [PY, f"{MAIN}/pipeline/yt_links.py", "--apply"],
    "hints": [PY, f"{MAIN}/pipeline/hints_resolve.py"],
}
p = subprocess.run(CMDS[sys.argv[1]], cwd=MAIN, capture_output=True, text=True)
print(p.stdout[-7000:])
if p.returncode:
    print("STDERR:", p.stderr[-2500:])
print("rc", p.returncode)
