"""Regression tests for the PR 4 code-review findings (CSV and error handling)."""

from pathlib import Path

import pytest

import claims
import db
from main import main

ROOT = Path(__file__).resolve().parent.parent
HEADER = "passenger,flight,type,tier,note"
T0 = "2026-11-02T14:00:00"


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    """Run main([...]) against a fresh temporary database; return (code, out, err)."""
    monkeypatch.setenv("ORBIT_DB", str(tmp_path / "hardening.db"))

    def _run(*args):
        capsys.readouterr()
        code = main(list(args))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def stored(sql):
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def write_csv(tmp_path, body, *, encoding="utf-8"):
    path = tmp_path / "seed.csv"
    path.write_bytes(body.encode(encoding))
    return path


# --- CSV column validation: malformed rows are rejected, never half-imported.
@pytest.mark.parametrize(
    ("bad_row", "reason"),
    [
        ("Grace,OA2,lost,gold,note with, an unquoted comma", "too many"),
        ("Grace,OA2,lost,gold", "too few"),
    ],
    ids=["too-many-columns", "too-few-columns"],
)
def test_seed_rejects_rows_with_the_wrong_number_of_columns(
    run, tmp_path, bad_row, reason
):
    path = write_csv(tmp_path, f"{HEADER}\nAda,OA1,lost,gold,fine\n{bad_row}\n")

    code, out, err = run("--now", T0, "seed", str(path))

    assert code == 1
    assert out == ""
    assert "row 2" in err
    assert "5 columns" in err
    assert "Traceback" not in err
    assert stored("SELECT * FROM claims") == []  # the valid first row rolled back
    assert stored("SELECT * FROM history") == []


# --- UTF-8 BOM (Excel's "CSV UTF-8") is accepted.
def test_seed_accepts_a_utf8_bom(run, tmp_path):
    path = write_csv(
        tmp_path, f"{HEADER}\nAda Lovelace,OA1,lost,gold,fine\n", encoding="utf-8-sig"
    )
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    code, out, err = run("--now", T0, "seed", str(path))

    assert (code, err) == (0, "")
    assert "1" in out
    assert [r["passenger"] for r in stored("SELECT passenger FROM claims")] == [
        "Ada Lovelace"
    ]


# --- Unreadable CSVs give a clean error, not a traceback.
def test_seed_reports_a_file_that_is_not_utf8(run, tmp_path):
    path = write_csv(
        tmp_path, f"{HEADER}\nJosé,OA1,lost,gold,fine\n", encoding="latin-1"
    )

    code, out, err = run("--now", T0, "seed", str(path))

    assert code == 1
    assert out == ""
    assert "seed.csv" in err
    assert "UTF-8" in err
    assert "Traceback" not in err
    assert stored("SELECT * FROM claims") == []


def test_seed_reports_a_csv_field_that_is_too_large(run, tmp_path):
    path = write_csv(tmp_path, f"{HEADER}\nAda,OA1,lost,gold,{'x' * 200_000}\n")

    code, out, err = run("--now", T0, "seed", str(path))

    assert code == 1
    assert out == ""
    assert err.startswith("error:")
    assert "Traceback" not in err
    assert stored("SELECT * FROM claims") == []


# --- Database problems give a clean error, not a traceback.
def test_unopenable_database_path_is_reported_cleanly(
    run, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ORBIT_DB", str(tmp_path / "missing-dir" / "x.db"))

    code, out, err = run("list")

    assert code == 1
    assert out == ""
    assert err.startswith("error:")
    assert "Traceback" not in err


# --- A blank note is stored the same way by `file` and `seed`.
def test_blank_notes_are_stored_as_null_by_file_and_seed(run, tmp_path):
    assert (
        run(
            "--now",
            T0,
            "file",
            "--passenger",
            "Filed Blank",
            "--flight",
            "OA1",
            "--type",
            "lost",
            "--tier",
            "gold",
            "--note",
            "",
        )[0]
        == 0
    )
    path = write_csv(tmp_path, f"{HEADER}\nSeeded Blank,OA2,lost,gold,\n")
    assert run("--now", T0, "seed", str(path))[0] == 0

    notes = {r["passenger"]: r["note"] for r in stored("SELECT * FROM claims")}
    assert notes == {"Filed Blank": None, "Seeded Blank": None}


# --- list filters reject unknown values (behavior added in this feature).
@pytest.mark.parametrize(
    ("filters", "field"),
    [({"status": "pending"}, "status"), ({"tier": "Gold"}, "tier")],
    ids=["unknown-status", "wrong-case-tier"],
)
def test_list_claims_rejects_unknown_filter_values(conn, filters, field):
    with pytest.raises(ValueError, match=field):
        claims.list_claims(conn, **filters)


@pytest.mark.parametrize("args", [("--status", "pending"), ("--tier", "platinum")])
def test_list_command_reports_unknown_filter_values(run, args):
    code, out, err = run("list", *args)

    assert code == 1
    assert out == ""
    assert args[0].lstrip("-") in err
    assert "Traceback" not in err


# --- .gitignore must end with a newline so later appends stay on their own line.
def test_gitignore_ends_with_a_newline_and_ignores_local_claude_file():
    data = (ROOT / ".gitignore").read_bytes()

    assert data.endswith(b"\n")
    assert b"CLAUDE.local.md\n" in data
