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


# The single lookup table for status changes: current status -> allowed next statuses.
TRANSITIONS = {
    "open": ("assigned", "escalated"),
    "assigned": ("resolved", "escalated"),
    "escalated": ("assigned", "resolved"),
    "resolved": ("closed",),
    "closed": (),
}


def _change_status(conn, claim_id, action, new_status, *, now, actor, agent=None):
    """Move a claim to new_status if TRANSITIONS allows it, logging history.

    The claim update and its history row share one transaction, so they
    succeed or fail together. `agent`, when given, is stored on the claim.
    """
    with conn:
        claim = get_claim(conn, claim_id)
        if claim is None:
            raise ValueError(f"cannot {action} claim {claim_id}: claim not found")
        old_status = claim["status"]
        if new_status not in TRANSITIONS[old_status]:
            raise ValueError(
                f"cannot {action} claim {claim_id}: status is {old_status}"
            )

        changed_at = now.isoformat(timespec="seconds")
        if agent is None:
            conn.execute(
                "UPDATE claims SET status = ? WHERE id = ?", (new_status, claim_id)
            )
        else:
            conn.execute(
                "UPDATE claims SET status = ?, agent = ? WHERE id = ?",
                (new_status, agent, claim_id),
            )
        conn.execute(
            "INSERT INTO history (claim_id, old_status, new_status, actor, changed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, old_status, new_status, actor, changed_at),
        )


def assign_claim(conn, claim_id, agent, *, now, actor="desk"):
    """Assign an agent to an open or escalated claim, making it assigned."""
    _require_text("agent", agent)
    _change_status(
        conn, claim_id, "assign", "assigned", now=now, actor=actor, agent=agent
    )


def resolve_claim(conn, claim_id, *, now, actor="desk"):
    """Resolve an assigned or escalated claim."""
    _change_status(conn, claim_id, "resolve", "resolved", now=now, actor=actor)


def close_claim(conn, claim_id, *, now, actor="desk"):
    """Close a resolved claim. Closed is final."""
    _change_status(conn, claim_id, "close", "closed", now=now, actor=actor)


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
