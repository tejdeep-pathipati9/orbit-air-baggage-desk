import sqlite3
from datetime import datetime, timedelta

import pytest

import claims

FILED = datetime(2026, 11, 2, 14, 0, 0)
WINDOWS = {"gold": timedelta(hours=4), "silver": timedelta(hours=24)}
MINUTE = timedelta(minutes=1)


def file_one(conn, tier="gold", filed=FILED):
    return claims.file_claim(
        conn,
        passenger="Ada Lovelace",
        flight="OA101",
        type="lost",
        tier=tier,
        note=None,
        now=filed,
    )


def deadline_of(conn, claim_id):
    return datetime.fromisoformat(claims.get_claim(conn, claim_id)["deadline"])


def claim_in_status(conn, status, tier="gold"):
    """File a claim and force it into `status` with plain SQL (setup only)."""
    claim_id = file_one(conn, tier=tier)
    agent = "agent.smith" if status in ("assigned", "resolved", "closed") else None
    conn.execute(
        "UPDATE claims SET status = ?, agent = ? WHERE id = ?",
        (status, agent, claim_id),
    )
    conn.commit()
    return claim_id


def snapshot(conn, claim_id):
    return (
        dict(claims.get_claim(conn, claim_id)),
        [dict(row) for row in claims.get_history(conn, claim_id)],
    )


# 1-3. Overdue rule: strictly after the deadline, never at it.
@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (-MINUTE, False),  # one minute before the deadline
        (timedelta(0), False),  # exactly at the deadline
        (MINUTE, True),  # one minute after the deadline
    ],
    ids=["one-minute-before", "exactly-at-deadline", "one-minute-after"],
)
@pytest.mark.parametrize("tier", ["gold", "silver"])
def test_is_overdue_only_strictly_after_the_deadline(conn, tier, offset, expected):
    claim = claims.get_claim(conn, file_one(conn, tier=tier))
    now = datetime.fromisoformat(claim["deadline"]) + offset

    assert claims.is_overdue(claim, now) is expected


def test_is_overdue_one_second_after_the_deadline(conn):
    claim = claims.get_claim(conn, file_one(conn))
    now = datetime.fromisoformat(claim["deadline"]) + timedelta(seconds=1)

    assert claims.is_overdue(claim, now) is True


def test_is_overdue_just_filed_is_not_overdue(conn):
    claim = claims.get_claim(conn, file_one(conn))

    assert claims.is_overdue(claim, FILED) is False


def test_is_overdue_uses_the_supplied_time_only(conn):
    claim = claims.get_claim(conn, file_one(conn))
    deadline = datetime.fromisoformat(claim["deadline"])

    # Same claim, different supplied times: the answer follows `now`, not a clock.
    assert claims.is_overdue(claim, deadline - MINUTE) is False
    assert claims.is_overdue(claim, deadline + MINUTE) is True
    assert claims.is_overdue(claim, deadline - MINUTE) is False


# The same boundary through escalation itself (stored text vs supplied time).
@pytest.mark.parametrize(
    ("offset", "escalated"),
    [(-MINUTE, False), (timedelta(0), False), (MINUTE, True)],
    ids=["one-minute-before", "exactly-at-deadline", "one-minute-after"],
)
def test_escalation_respects_the_deadline_boundary(conn, offset, escalated):
    claim_id = file_one(conn)
    now = deadline_of(conn, claim_id) + offset

    result = claims.escalate_overdue(conn, now=now)

    assert result == ([claim_id] if escalated else [])
    expected_status = "escalated" if escalated else "open"
    assert claims.get_claim(conn, claim_id)["status"] == expected_status


# 4. Escalation moves overdue open/assigned claims, writes history, returns ids.
@pytest.mark.parametrize("status", ["open", "assigned"])
def test_escalation_moves_overdue_claim_and_returns_its_id(conn, status):
    claim_id = claim_in_status(conn, status)
    now = deadline_of(conn, claim_id) + MINUTE

    result = claims.escalate_overdue(conn, now=now)

    assert result == [claim_id]
    claim = claims.get_claim(conn, claim_id)
    assert claim["status"] == "escalated"
    assert claim["agent"] == ("agent.smith" if status == "assigned" else None)
    history = claims.get_history(conn, claim_id)
    assert history[-1]["old_status"] == status
    assert history[-1]["new_status"] == "escalated"


def test_escalation_returns_every_eligible_id_in_order(conn):
    first = file_one(conn)
    second = claim_in_status(conn, "assigned")
    third = file_one(conn)
    now = FILED + timedelta(hours=5)  # gold deadline is +4h

    assert claims.escalate_overdue(conn, now=now) == [first, second, third]


def test_escalation_with_nothing_overdue_returns_empty_list(conn):
    file_one(conn)

    assert claims.escalate_overdue(conn, now=FILED) == []


def test_escalation_handles_mixed_tiers_by_their_own_deadlines(conn):
    gold = file_one(conn, tier="gold")  # due +4h
    silver = file_one(conn, tier="silver")  # due +24h
    now = FILED + timedelta(hours=5)

    assert claims.escalate_overdue(conn, now=now) == [gold]
    assert claims.get_claim(conn, silver)["status"] == "open"


# 5. Escalation leaves other claims unchanged.
@pytest.mark.parametrize("status", ["resolved", "closed", "escalated"])
def test_overdue_claims_in_ineligible_status_are_left_alone(conn, status):
    claim_id = claim_in_status(conn, status)
    now = deadline_of(conn, claim_id) + timedelta(days=10)
    before = snapshot(conn, claim_id)

    result = claims.escalate_overdue(conn, now=now)

    assert result == []
    assert snapshot(conn, claim_id) == before


@pytest.mark.parametrize("status", ["open", "assigned"])
def test_claims_not_yet_overdue_are_left_alone(conn, status):
    claim_id = claim_in_status(conn, status)
    now = deadline_of(conn, claim_id)  # exactly at the deadline
    before = snapshot(conn, claim_id)

    result = claims.escalate_overdue(conn, now=now)

    assert result == []
    assert snapshot(conn, claim_id) == before


def test_only_eligible_claims_are_escalated_in_a_mixed_batch(conn):
    open_overdue = claim_in_status(conn, "open")
    assigned_overdue = claim_in_status(conn, "assigned")
    resolved = claim_in_status(conn, "resolved")
    closed = claim_in_status(conn, "closed")
    already_escalated = claim_in_status(conn, "escalated")
    not_overdue = claim_in_status(conn, "open", tier="silver")
    untouched = [resolved, closed, already_escalated, not_overdue]
    before = {claim_id: snapshot(conn, claim_id) for claim_id in untouched}
    now = FILED + timedelta(hours=5)  # past gold (+4h), before silver (+24h)

    result = claims.escalate_overdue(conn, now=now)

    assert result == [open_overdue, assigned_overdue]
    for claim_id in untouched:
        assert snapshot(conn, claim_id) == before[claim_id]


def test_running_escalation_twice_does_not_escalate_again(conn):
    claim_id = file_one(conn)
    now = FILED + timedelta(hours=5)

    assert claims.escalate_overdue(conn, now=now) == [claim_id]
    history_after_first = claims.get_history(conn, claim_id)

    assert claims.escalate_overdue(conn, now=now + MINUTE) == []
    assert len(claims.get_history(conn, claim_id)) == len(history_after_first)


# 6. Escalation history.
def test_escalation_writes_a_complete_history_row(conn):
    claim_id = file_one(conn)
    now = FILED + timedelta(hours=5)

    claims.escalate_overdue(conn, now=now)

    history = claims.get_history(conn, claim_id)
    assert len(history) == 2  # filing row + escalation row
    row = history[-1]
    assert row["claim_id"] == claim_id
    assert row["old_status"] == "open"
    assert row["new_status"] == "escalated"
    assert row["actor"] == "system"
    assert row["changed_at"] == now.isoformat()


def test_escalation_records_a_custom_actor(conn):
    claim_id = file_one(conn)

    claims.escalate_overdue(conn, now=FILED + timedelta(hours=5), actor="scheduler")

    assert claims.get_history(conn, claim_id)[-1]["actor"] == "scheduler"


def test_each_escalated_claim_gets_exactly_one_history_row(conn):
    ids = [file_one(conn), claim_in_status(conn, "assigned")]
    now = FILED + timedelta(hours=5)

    claims.escalate_overdue(conn, now=now)

    for claim_id in ids:
        escalations = [
            row
            for row in claims.get_history(conn, claim_id)
            if row["new_status"] == "escalated"
        ]
        assert len(escalations) == 1


def test_escalated_claim_history_continues_in_order_with_later_changes(conn):
    claim_id = file_one(conn)
    t_assign = FILED + timedelta(hours=1)
    t_escalate = FILED + timedelta(hours=5)
    t_resolve = FILED + timedelta(hours=6)

    claims.assign_claim(conn, claim_id, "agent.smith", now=t_assign)
    claims.escalate_overdue(conn, now=t_escalate)
    claims.resolve_claim(conn, claim_id, now=t_resolve, actor="agent.smith")

    steps = [
        (row["old_status"], row["new_status"], row["actor"], row["changed_at"])
        for row in claims.get_history(conn, claim_id)
    ]
    assert steps == [
        (None, "open", "desk", FILED.isoformat()),
        ("open", "assigned", "desk", t_assign.isoformat()),
        ("assigned", "escalated", "system", t_escalate.isoformat()),
        ("escalated", "resolved", "agent.smith", t_resolve.isoformat()),
    ]


# 7. Rollback: a failing history insert leaves claims unchanged.
def make_history_inserts_fail(conn):
    conn.execute(
        "CREATE TRIGGER fail_history BEFORE INSERT ON history"
        " BEGIN SELECT RAISE(ABORT, 'history unavailable'); END"
    )
    conn.commit()


@pytest.mark.parametrize("status", ["open", "assigned"])
def test_failing_history_insert_rolls_back_the_escalation(conn, status):
    claim_id = claim_in_status(conn, status)
    make_history_inserts_fail(conn)
    before = snapshot(conn, claim_id)
    now = deadline_of(conn, claim_id) + MINUTE

    with pytest.raises(sqlite3.Error, match="history unavailable"):
        claims.escalate_overdue(conn, now=now)

    assert snapshot(conn, claim_id) == before
    assert claims.get_claim(conn, claim_id)["status"] == status
    assert not conn.in_transaction


def test_failing_history_insert_leaves_every_overdue_claim_unchanged(conn):
    ids = [file_one(conn), claim_in_status(conn, "assigned"), file_one(conn)]
    make_history_inserts_fail(conn)
    before = {claim_id: snapshot(conn, claim_id) for claim_id in ids}

    with pytest.raises(sqlite3.Error):
        claims.escalate_overdue(conn, now=FILED + timedelta(hours=5))

    for claim_id in ids:
        assert snapshot(conn, claim_id) == before[claim_id]
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 3


def test_history_failure_on_the_last_claim_rolls_back_the_whole_batch(conn):
    # Claims 1 and 2 escalate cleanly; only the final claim's history insert fails.
    first = file_one(conn)
    second = claim_in_status(conn, "assigned")
    last = file_one(conn)
    conn.execute(
        "CREATE TRIGGER fail_last_history BEFORE INSERT ON history"
        f" WHEN NEW.claim_id = {last} AND NEW.new_status = 'escalated'"
        " BEGIN SELECT RAISE(ABORT, 'history unavailable'); END"
    )
    conn.commit()
    ids = [first, second, last]
    before = {claim_id: snapshot(conn, claim_id) for claim_id in ids}

    with pytest.raises(sqlite3.Error, match="history unavailable"):
        claims.escalate_overdue(conn, now=FILED + timedelta(hours=5))

    # The earlier claims must not stay escalated, and none may have new history.
    for claim_id in ids:
        assert snapshot(conn, claim_id) == before[claim_id]
    assert not conn.in_transaction


def test_successful_escalation_is_committed_for_other_connections(conn, tmp_path):
    import db

    claim_id = file_one(conn)

    claims.escalate_overdue(conn, now=FILED + timedelta(hours=5))

    other = db.connect(tmp_path / "test.db")
    try:
        status = other.execute(
            "SELECT status FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()["status"]
        count = other.execute(
            "SELECT COUNT(*) FROM history WHERE claim_id = ?", (claim_id,)
        ).fetchone()[0]
    finally:
        other.close()
    assert status == "escalated"
    assert count == 2
