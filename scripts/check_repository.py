"""Small pre-commit hygiene check; not a replacement for a full secret scanner."""

import re
import subprocess
from pathlib import Path

files = subprocess.check_output(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard"], text=True
).splitlines()
pattern = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[0-9]{8,}:[A-Za-z0-9_-]{30,}"
)
hits = [
    p
    for p in files
    if Path(p).is_file()
    and pattern.search(Path(p).read_bytes().decode("utf-8", errors="ignore"))
]
artifacts = [
    p for p in files if p.endswith((".sqlite3", ".db", ".log", ".tmp", ".env"))
]
print("Secret-pattern file matches:", hits)
print("Runtime artifacts:", artifacts)
raise SystemExit(bool(hits or artifacts))
