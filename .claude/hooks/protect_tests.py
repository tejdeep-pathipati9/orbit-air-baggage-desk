# STRETCH GOAL 1
"""Block Claude Edit/Write calls targeting this project's tests directory."""

import json
import os
import sys
from pathlib import Path


def main():
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or not isinstance(event.get("tool_input"), dict):
            raise TypeError("expected a tool event")
        file_path = event["tool_input"].get("file_path")
        if not isinstance(file_path, str):
            raise TypeError("expected a file_path string")
        if not file_path:
            raise ValueError("missing file_path")
        root = Path(
            os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2])
        ).resolve()
        cwd = root / Path(event.get("cwd") or root)
        target = (cwd / file_path).resolve()
        # Missing Write targets are valid; other lookup failures must block.
        try:
            target.stat()
        except FileNotFoundError:
            pass
        protected = (root / "tests").resolve()
    except (ValueError, TypeError, OSError, RuntimeError):
        print("Invalid hook input; edit blocked.", file=sys.stderr)
        return 2

    if target.is_relative_to(protected):
        print("Blocked: tests/ is protected from Claude edits.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
