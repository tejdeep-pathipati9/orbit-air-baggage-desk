from datetime import timedelta

from db import TIERS, TYPES

RESPONSE_WINDOWS = {
    "gold": timedelta(hours=4),
    "silver": timedelta(hours=24),
    "standard": timedelta(hours=72),
}


def _require_text(field, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must not be blank")


def _require_choice(field, value, allowed):
    if value not in allowed:
        raise ValueError(
            f"invalid {field} {value!r}: must be one of {', '.join(allowed)}"
        )


def file_claim(conn, passenger, flight, type, tier, note=None, *, now, actor="desk"):
    """File a claim as open, record the filing in history, and return its id."""
    _require_text("passenger", passenger)
    _require_text("flight", flight)
    _require_choice("type", type, TYPES)
    _require_choice("tier", tier, TIERS)

    filed_at = now.isoformat(timespec="seconds")
    deadline = (now + RESPONSE_WINDOWS[tier]).isoformat(timespec="seconds")

    with conn:
        cursor = conn.execute(
            "INSERT INTO claims (passenger, flight, type, tier, status, note,"
            " filed_at, deadline) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
            (passenger, flight, type, tier, note, filed_at, deadline),
        )
        claim_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO history (claim_id, old_status, new_status, actor, changed_at)"
            " VALUES (?, NULL, 'open', ?, ?)",
            (claim_id, actor, filed_at),
        )
    return claim_id


def get_claim(conn, claim_id):
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def get_history(conn, claim_id):
    return conn.execute(
        "SELECT * FROM history WHERE claim_id = ? ORDER BY id", (claim_id,)
    ).fetchall()


def list_claims(conn, status=None, tier=None):
    clauses, params = [], []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(f"SELECT * FROM claims{where} ORDER BY id", params).fetchall()
