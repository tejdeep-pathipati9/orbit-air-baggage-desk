#!/bin/sh
# Stop hook: run pytest when Claude finishes. Exit 2 on failures so Claude keeps working.

# Consume the event without using its flags to bypass verification.
cat > /dev/null

if ! cd "${CLAUDE_PROJECT_DIR:-.}"; then
  echo "Cannot enter the project directory. Stop blocked." >&2
  exit 2
fi

# Prefer the project's installed dependencies when no environment is activated.
if [ -x .venv/bin/python ]; then
  out=$(.venv/bin/python -m pytest -q 2>&1)
else
  out=$(python3 -m pytest -q 2>&1)
fi
status=$?

case $status in
  0) exit 0 ;;
  *)
    echo "pytest failed (exit $status). Fix the failures before stopping." >&2
    echo "$out" | tail -n 40 >&2
    exit 2
    ;;
esac
