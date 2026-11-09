from datetime import datetime, timedelta

from db import STATUSES, TIERS, TYPES

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


def _insert_claim(conn, passenger, flight, type, tier, note, *, now, actor):
    """Validate and insert one open claim plus its filing history row.

    Runs inside the caller's transaction and never commits.
    """
    _require_text("passenger", passenger)
    _require_text("flight", flight)
    _require_choice("type", type, TYPES)
    _require_choice("tier", tier, TIERS)

    filed_at = now.isoformat(timespec="seconds")
    deadline = (now + RESPONSE_WINDOWS[tier]).isoformat(timespec="seconds")

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


def file_claim(conn, passenger, flight, type, tier, note=None, *, now, actor="desk"):
    """File a claim as open, record the filing in history, and return its id."""
    with conn:
        return _insert_claim(
            conn, passenger, flight, type, tier, note, now=now, actor=actor
        )


def seed_claims(conn, rows, *, now, actor="seed"):
    """File every row (dicts with the claim fields) in one transaction.

    If any row is invalid nothing is imported and the error names the row.
    Returns the new claim ids.
    """
    ids = []
    with conn:
        for number, row in enumerate(rows, start=1):
            try:
                ids.append(
                    _insert_claim(
                        conn,
                        row.get("passenger"),
                        row.get("flight"),
                        row.get("type"),
                        row.get("tier"),
                        row.get("note") or None,
                        now=now,
                        actor=actor,
                    )
                )
            except ValueError as exc:
                raise ValueError(f"seed row {number}: {exc}") from exc
    return ids


# The single lookup table for status changes: current status -> allowed next statuses.
TRANSITIONS = {
    "open": ("assigned", "escalated"),
    "assigned": ("resolved", "escalated"),
    "escalated": ("assigned", "resolved"),
    "resolved": ("closed",),
    "closed": (),
}


def _apply_status_change(conn, claim_id, action, new_status, *, now, actor, agent=None):
    """Move a claim to new_status if TRANSITIONS allows it, logging history.

    Runs inside the caller's transaction and never commits, so callers decide
    what succeeds or fails together. `agent`, when given, is stored on the claim.
    """
    claim = get_claim(conn, claim_id)
    if claim is None:
        raise ValueError(f"cannot {action} claim {claim_id}: claim not found")
    old_status = claim["status"]
    if new_status not in TRANSITIONS[old_status]:
        raise ValueError(f"cannot {action} claim {claim_id}: status is {old_status}")

    changed_at = now.isoformat(timespec="seconds")
    # Guard on the status we validated: if another connection changed the
    # claim since we read it, nothing matches and we refuse the change.
    updated = conn.execute(
        "UPDATE claims SET status = ?, agent = COALESCE(?, agent)"
        " WHERE id = ? AND status = ?",
        (new_status, agent, claim_id, old_status),
    )
    if updated.rowcount != 1:
        raise ValueError(
            f"cannot {action} claim {claim_id}: status changed concurrently"
        )
    conn.execute(
        "INSERT INTO history (claim_id, old_status, new_status, actor, changed_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (claim_id, old_status, new_status, actor, changed_at),
    )


def _change_status(conn, claim_id, action, new_status, *, now, actor, agent=None):
    """Change one claim's status and log it in a single transaction."""
    with conn:
        _apply_status_change(
            conn, claim_id, action, new_status, now=now, actor=actor, agent=agent
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


def is_overdue(claim, now):
    """A claim is overdue only when now is strictly after its deadline."""
    return now > datetime.fromisoformat(claim["deadline"])


def escalate_overdue(conn, *, now, actor="system"):
    """Escalate every overdue open or assigned claim and return their ids.

    The whole run is one transaction: every status update and its history row
    commit together, and if any write fails nothing is escalated.
    """
    escalated = []
    with conn:
        candidates = conn.execute(
            "SELECT * FROM claims WHERE status IN ('open', 'assigned') ORDER BY id"
        ).fetchall()
        for claim in candidates:
            if is_overdue(claim, now):
                _apply_status_change(
                    conn, claim["id"], "escalate", "escalated", now=now, actor=actor
                )
                escalated.append(claim["id"])
    return escalated


def get_claim(conn, claim_id):
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def get_history(conn, claim_id):
    return conn.execute(
        "SELECT * FROM history WHERE claim_id = ? ORDER BY id", (claim_id,)
    ).fetchall()


def list_claims(conn, status=None, tier=None):
    clauses, params = [], []
    if status is not None:
        _require_choice("status", status, STATUSES)
        clauses.append("status = ?")
        params.append(status)
    if tier is not None:
        _require_choice("tier", tier, TIERS)
        clauses.append("tier = ?")
        params.append(tier)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(f"SELECT * FROM claims{where} ORDER BY id", params).fetchall()
