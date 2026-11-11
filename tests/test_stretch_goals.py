"""Focused checks for tier reporting and the Claude test-edit hook."""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import claims
from main import main

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 2, 14)


def test_report_includes_empty_tiers(conn):
    assert claims.claim_report(conn, now=T0) == [
        {"tier": tier, "open": 0, "overdue": 0}
        for tier in ("standard", "silver", "gold")
    ]


def test_report_counts_unresolved_claims_without_changing_data(conn):
    for tier, statuses in (
        ("gold", ["open", "open", "assigned", "escalated", "resolved", "closed"]),
        ("silver", ["open"]),
        ("standard", ["open"]),
    ):
        for status in statuses:
            claim_id = claims.file_claim(conn, "Ada", "OA1", "lost", tier, now=T0)
            # Fixture states isolate reporting from status-transition behavior.
            with conn:
                conn.execute(
                    "UPDATE claims SET status = ? WHERE id = ?", (status, claim_id)
                )
    before = [tuple(row) for row in conn.execute("SELECT * FROM claims")]
    history = [tuple(row) for row in conn.execute("SELECT * FROM history")]

    assert claims.claim_report(conn, now=T0 + timedelta(hours=24)) == [
        {"tier": "standard", "open": 1, "overdue": 0},
        {"tier": "silver", "open": 1, "overdue": 0},
        {"tier": "gold", "open": 2, "overdue": 4},
    ]
    assert [tuple(row) for row in conn.execute("SELECT * FROM claims")] == before
    assert [tuple(row) for row in conn.execute("SELECT * FROM history")] == history


@pytest.mark.parametrize("minutes, overdue", [(-1, 0), (0, 0), (1, 1)])
def test_report_respects_the_deadline(conn, minutes, overdue):
    claims.file_claim(conn, "Ada", "OA1", "lost", "gold", now=T0)

    rows = claims.claim_report(conn, now=T0 + timedelta(hours=4, minutes=minutes))

    assert rows[-1] == {"tier": "gold", "open": 1, "overdue": overdue}


def test_report_command_uses_now_and_prints_all_tiers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORBIT_DB", str(tmp_path / "report.db"))
    assert (
        main(
            [
                "--now",
                T0.isoformat(),
                "file",
                "--passenger",
                "Ada",
                "--flight",
                "OA1",
                "--type",
                "lost",
                "--tier",
                "gold",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--now", "2026-11-02T18:01:00", "report"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert [line.split() for line in captured.out.splitlines()] == [
        ["Tier", "Open", "Overdue"],
        ["standard", "0", "0"],
        ["silver", "0", "0"],
        ["gold", "1", "1"],
    ]


def run_hook(payload):
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    group = next(
        item
        for item in settings["hooks"]["PreToolUse"]
        if set(item["matcher"].split("|")) == {"Edit", "Write"}
    )
    return subprocess.run(
        ["sh", "-c", group["hooks"][0]["command"]],
        check=False,
        input=payload,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("path_kind", ["relative", "absolute", "symlink"])
def test_hook_blocks_test_edits(path_kind, tmp_path):
    target = ROOT / "tests/test_cli.py"
    before = target.read_bytes()
    if path_kind == "relative":
        path = "test_cli.py"
        cwd = ROOT / "tests"
    elif path_kind == "absolute":
        path, cwd = str(target), ROOT
    else:
        alias = tmp_path / "linked-tests"
        alias.symlink_to(ROOT / "tests", target_is_directory=True)
        path, cwd = str(alias / "test_cli.py"), ROOT
    result = run_hook(
        json.dumps(
            {
                "tool_name": "Write" if path_kind == "absolute" else "Edit",
                "cwd": str(cwd),
                "tool_input": {"file_path": path},
            }
        )
    )

    assert result.returncode == 2
    assert "tests/ is protected" in result.stderr
    assert target.read_bytes() == before


@pytest.mark.parametrize("path", ["main.py", "tests_extra/example.py"])
def test_hook_allows_files_outside_tests(path):
    result = run_hook(
        json.dumps(
            {
                "tool_name": "Edit",
                "cwd": str(ROOT),
                "tool_input": {"file_path": path},
            }
        )
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_hook_blocks_malformed_input():
    result = run_hook("not json")

    assert result.returncode == 2
    assert "Invalid hook input" in result.stderr
