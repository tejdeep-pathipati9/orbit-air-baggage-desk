#!/bin/sh
# Stop hook: run pytest when Claude finishes. Exit 2 on failures so Claude keeps working.
#
# - Already continuing from a previous Stop block: exit 0 (prevents infinite retries).
# - Intentional TDD red stage: `touch .claude/tdd-red` to skip; delete it when going green.
# - No tests collected (pytest exit 5) or pytest missing: allow.

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# stop_hook_active is true when Claude is already continuing because of a Stop hook.
if cat | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

if [ -f .claude/tdd-red ]; then
  echo "TDD red stage (.claude/tdd-red present): skipping pytest." >&2
  exit 0
fi

out=$(python3 -m pytest -q 2>&1)
status=$?

case $status in
  0|5) exit 0 ;;  # passed, or no tests collected yet
  *)
    echo "pytest failed (exit $status). Fix the failures, or 'touch .claude/tdd-red' if this is an intentional TDD red stage." >&2
    echo "$out" | tail -n 40 >&2
    exit 2
    ;;
esac
