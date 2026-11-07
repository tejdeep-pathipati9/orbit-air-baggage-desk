import json
import sys
import subprocess
from pathlib import Path

data = json.load(sys.stdin)
prompt = data.get("prompt", "")

if prompt:
    root = Path(__file__).resolve().parents[2]
    branch = subprocess.getoutput(f"git -C '{root}' branch --show-current")
    log = root / ".claude/prompt-log.md"

    with log.open("a") as f:
        f.write(f"\n### {branch}\n\n{prompt}\n\n---\n")