"""The Stop hook must run pytest and block every unsuccessful result."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude/hooks/run-pytest.sh"


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "pytest.ini").write_text("[pytest]\n")
    return folder


def run_hook(project, active=False):
    return subprocess.run(
        ["sh", str(HOOK)],
        input=json.dumps({"stop_hook_active": active}),
        cwd=project.parent,
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(project),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize(
    "active, marker", [(False, False), (True, False), (False, True), (True, True)]
)
def test_stop_blocks_failing_tests_even_with_old_bypass_flags(project, active, marker):
    (project / "test_sample.py").write_text("def test_sample():\n    assert False\n")
    if marker:
        (project / ".claude").mkdir()
        (project / ".claude/tdd-red").touch()

    result = run_hook(project, active=active)

    assert result.returncode == 2
    assert "1 failed" in result.stderr


def test_stop_runs_passing_tests_even_with_old_bypass_flags(project):
    (project / "test_sample.py").write_text(
        "from pathlib import Path\n"
        "def test_sample():\n"
        "    Path('ran.txt').write_text('pytest ran')\n"
    )
    (project / ".claude").mkdir()
    (project / ".claude/tdd-red").touch()

    result = run_hook(project, active=True)

    assert result.returncode == 0
    assert result.stderr == ""
    assert (project / "ran.txt").read_text() == "pytest ran"


def test_stop_blocks_when_no_tests_are_collected(project):
    result = run_hook(project)

    assert result.returncode == 2
    assert "no tests ran" in result.stderr


def test_stop_blocks_when_project_directory_is_missing(project):
    result = run_hook(project / "missing")

    assert result.returncode == 2
