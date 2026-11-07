import sqlite3
from datetime import datetime, timedelta

import pytest

import claims

NOW = datetime(2026, 11, 2, 14, 0, 0)

STATUSES = ["open", "assigned", "escalated", "resolved", "closed"]

# Exactly the transitions from PLAN.md section 2.4.
EXPECTED_TRANSITIONS = {
    "open": {"assigned", "escalated"},
    "assigned": {"resolved", "escalated"},
    "escalated": {"assigned", "resolved"},
    "resolved": {"closed"},
    "closed": set(),
}

# Operation name -> (callable, status the operation produces, allowed from).
OPERATIONS = {
    "assign": (
        lambda conn, cid, **kw: claims.assign_claim(conn, cid, "agent.smith", **kw),
        "assigned",
        {"open", "escalated"},
    ),
    "resolve": (
        lambda conn, cid, **kw: claims.resolve_claim(conn, cid, **kw),
        "resolved",
        {"assigned", "escalated"},
    ),
    "close": (
        lambda conn, cid, **kw: claims.close_claim(conn, cid, **kw),
        "closed",
        {"resolved"},
    ),
}


def file_one(conn):
    return claims.file_claim(
        conn,
        passenger="Ada Lovelace",
        flight="OA101",
        type="lost",
        tier="standard",
        note=None,
        now=NOW,
    )


def claim_in_status(conn, status):
    """File a claim and force it into `status` with plain SQL (setup only)."""
    claim_id = file_one(conn)
    agent = "agent.smith" if status in ("assigned", "resolved", "closed") else None
    conn.execute(
        "UPDATE claims SET status = ?, agent = ? WHERE id = ?",
        (status, agent, claim_id),
    )
    conn.commit()
    return claim_id


def escalate_sql(conn, claim_id, *, changed_at, actor="system"):
    """Stand-in for the later escalate feature: change status and log history."""
    old = claims.get_claim(conn, claim_id)["status"]
    with conn:
        conn.execute("UPDATE claims SET status = 'escalated' WHERE id = ?", (claim_id,))
        conn.execute(
            "INSERT INTO history (claim_id, old_status, new_status, actor, changed_at)"
            " VALUES (?, ?, 'escalated', ?, ?)",
            (claim_id, old, actor, changed_at),
        )


def snapshot(conn, claim_id):
    return (
        dict(claims.get_claim(conn, claim_id)),
        [dict(row) for row in claims.get_history(conn, claim_id)],
    )


# 1. Assigning an open claim sets assigned and stores the agent.
def test_assign_open_claim_sets_assigned_and_stores_agent(conn):
    claim_id = file_one(conn)
    later = NOW + timedelta(minutes=5)

    claims.assign_claim(conn, claim_id, "agent.smith", now=later, actor="supervisor")

    claim = claims.get_claim(conn, claim_id)
    assert claim["status"] == "assigned"
    assert claim["agent"] == "agent.smith"
    history = claims.get_history(conn, claim_id)
    assert len(history) == 2
    assert history[-1]["old_status"] == "open"
    assert history[-1]["new_status"] == "assigned"
    assert history[-1]["actor"] == "supervisor"
    assert history[-1]["changed_at"] == later.isoformat()


@pytest.mark.parametrize("agent", ["", "   ", "\t", None])
def test_assign_requires_a_non_blank_agent(conn, agent):
    claim_id = file_one(conn)
    before = snapshot(conn, claim_id)

    with pytest.raises(ValueError, match="agent"):
        claims.assign_claim(conn, claim_id, agent, now=NOW)

    assert snapshot(conn, claim_id) == before


def test_default_actor_is_desk(conn):
    claim_id = file_one(conn)

    claims.assign_claim(conn, claim_id, "agent.smith", now=NOW)

    assert claims.get_history(conn, claim_id)[-1]["actor"] == "desk"


# 2. Assigning an escalated claim sets assigned.
def test_assign_escalated_claim_sets_assigned_and_stores_agent(conn):
    claim_id = claim_in_status(conn, "escalated")

    claims.assign_claim(conn, claim_id, "agent.jones", now=NOW)

    claim = claims.get_claim(conn, claim_id)
    assert claim["status"] == "assigned"
    assert claim["agent"] == "agent.jones"
    history = claims.get_history(conn, claim_id)
    assert history[-1]["old_status"] == "escalated"
    assert history[-1]["new_status"] == "assigned"


# 3. Resolving sets resolved.
@pytest.mark.parametrize("status", ["assigned", "escalated"])
def test_resolve_sets_resolved(conn, status):
    claim_id = claim_in_status(conn, status)
    later = NOW + timedelta(hours=1)

    claims.resolve_claim(conn, claim_id, now=later, actor="agent.smith")

    claim = claims.get_claim(conn, claim_id)
    assert claim["status"] == "resolved"
    history = claims.get_history(conn, claim_id)
    assert history[-1]["old_status"] == status
    assert history[-1]["new_status"] == "resolved"
    assert history[-1]["actor"] == "agent.smith"
    assert history[-1]["changed_at"] == later.isoformat()


# 4. Closing a resolved claim sets closed.
def test_close_resolved_claim_sets_closed(conn):
    claim_id = claim_in_status(conn, "resolved")

    claims.close_claim(conn, claim_id, now=NOW)

    assert claims.get_claim(conn, claim_id)["status"] == "closed"
    history = claims.get_history(conn, claim_id)
    assert history[-1]["old_status"] == "resolved"
    assert history[-1]["new_status"] == "closed"


# All permitted transitions work; the table is exactly the spec.
def test_transition_table_matches_the_spec_exactly(conn):
    assert {k: set(v) for k, v in claims.TRANSITIONS.items()} == EXPECTED_TRANSITIONS


@pytest.mark.parametrize(
    ("operation", "start"),
    [
        (name, start)
        for name, (_, _, allowed) in OPERATIONS.items()
        for start in sorted(allowed)
    ],
)
def test_every_permitted_transition_works(conn, operation, start):
    run, target, _ = OPERATIONS[operation]
    claim_id = claim_in_status(conn, start)

    run(conn, claim_id, now=NOW)

    assert claims.get_claim(conn, claim_id)["status"] == target
    assert target in EXPECTED_TRANSITIONS[start]


# 5. Disallowed changes are rejected and leave the claim unchanged.
@pytest.mark.parametrize(
    ("operation", "start"),
    [
        (name, start)
        for name, (_, _, allowed) in OPERATIONS.items()
        for start in STATUSES
        if start not in allowed
    ],
)
def test_disallowed_change_is_rejected_and_leaves_claim_unchanged(
    conn, operation, start
):
    run, _, _ = OPERATIONS[operation]
    claim_id = claim_in_status(conn, start)
    before = snapshot(conn, claim_id)

    with pytest.raises(ValueError) as excinfo:
        run(conn, claim_id, now=NOW)

    message = str(excinfo.value)
    assert f"claim {claim_id}" in message
    assert f"status is {start}" in message
    assert operation in message
    assert snapshot(conn, claim_id) == before


def test_assigning_an_already_assigned_claim_is_rejected(conn):
    claim_id = claim_in_status(conn, "assigned")

    with pytest.raises(ValueError, match="status is assigned"):
        claims.assign_claim(conn, claim_id, "someone.else", now=NOW)

    assert claims.get_claim(conn, claim_id)["agent"] == "agent.smith"


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_unknown_claim_is_rejected(conn, operation):
    run, _, _ = OPERATIONS[operation]

    with pytest.raises(ValueError, match="999"):
        run(conn, 999, now=NOW)


# 6. History lists every change in order with complete fields.
def test_history_lists_every_change_in_order_with_complete_fields(conn):
    claim_id = file_one(conn)
    t1, t2, t3, t4, t5 = (NOW + timedelta(hours=h) for h in range(1, 6))

    claims.assign_claim(conn, claim_id, "agent.smith", now=t1, actor="supervisor")
    escalate_sql(conn, claim_id, changed_at=t2.isoformat())
    claims.assign_claim(conn, claim_id, "agent.jones", now=t3, actor="supervisor")
    claims.resolve_claim(conn, claim_id, now=t4, actor="agent.jones")
    claims.close_claim(conn, claim_id, now=t5)

    history = claims.get_history(conn, claim_id)
    steps = [
        (row["old_status"], row["new_status"], row["actor"], row["changed_at"])
        for row in history
    ]
    assert steps == [
        (None, "open", "desk", NOW.isoformat()),
        ("open", "assigned", "supervisor", t1.isoformat()),
        ("assigned", "escalated", "system", t2.isoformat()),
        ("escalated", "assigned", "supervisor", t3.isoformat()),
        ("assigned", "resolved", "agent.jones", t4.isoformat()),
        ("resolved", "closed", "desk", t5.isoformat()),
    ]
    assert all(row["claim_id"] == claim_id for row in history)
    assert [row["id"] for row in history] == sorted(row["id"] for row in history)
    assert claims.get_claim(conn, claim_id)["agent"] == "agent.jones"


def test_history_is_kept_separately_per_claim(conn):
    first = file_one(conn)
    second = file_one(conn)

    claims.assign_claim(conn, first, "agent.smith", now=NOW)

    assert len(claims.get_history(conn, first)) == 2
    assert len(claims.get_history(conn, second)) == 1


# Atomic claim/history persistence (issue: transactions succeed or fail together).
@pytest.mark.parametrize(
    ("operation", "start"),
    [
        ("assign", "open"),
        ("assign", "escalated"),
        ("resolve", "assigned"),
        ("resolve", "escalated"),
        ("close", "resolved"),
    ],
)
def test_failing_history_insert_rolls_back_the_claim_change(conn, operation, start):
    run, _, _ = OPERATIONS[operation]
    claim_id = claim_in_status(conn, start)
    conn.execute(
        "CREATE TRIGGER fail_history BEFORE INSERT ON history"
        " BEGIN SELECT RAISE(ABORT, 'history unavailable'); END"
    )
    conn.commit()
    before = snapshot(conn, claim_id)

    with pytest.raises(sqlite3.Error, match="history unavailable"):
        run(conn, claim_id, now=NOW)

    assert snapshot(conn, claim_id) == before
    assert not conn.in_transaction


def test_successful_change_is_committed_and_visible_to_other_connections(
    conn, tmp_path
):
    import db

    claim_id = file_one(conn)

    claims.assign_claim(conn, claim_id, "agent.smith", now=NOW)

    other = db.connect(tmp_path / "test.db")
    try:
        row = other.execute(
            "SELECT status, agent FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        count = other.execute(
            "SELECT COUNT(*) FROM history WHERE claim_id = ?", (claim_id,)
        ).fetchone()[0]
    finally:
        other.close()
    assert (row["status"], row["agent"]) == ("assigned", "agent.smith")
    assert count == 2
