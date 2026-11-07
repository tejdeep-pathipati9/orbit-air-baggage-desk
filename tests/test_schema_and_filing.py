import sqlite3
from datetime import datetime, timedelta

import pytest

import claims

NOW = datetime(2026, 11, 2, 14, 0, 0)


def file_one(conn, **overrides):
    fields = {
        "passenger": "Ada Lovelace",
        "flight": "OA101",
        "type": "lost",
        "tier": "standard",
        "note": None,
    }
    fields.update(overrides)
    return claims.file_claim(conn, now=NOW, **fields)


def insert_claim_sql(conn, **overrides):
    row = {
        "passenger": "Ada Lovelace",
        "flight": "OA101",
        "type": "lost",
        "tier": "standard",
        "status": "open",
        "filed_at": "2026-11-02T14:00:00",
        "deadline": "2026-11-05T14:00:00",
    }
    row.update(overrides)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return conn.execute(
        f"INSERT INTO claims ({columns}) VALUES ({marks})", list(row.values())
    )


# 1. A valid claim is stored as open with correct fields and a filing history row.
def test_valid_claim_is_stored_open_with_filing_history(conn):
    claim_id = file_one(
        conn,
        passenger="Grace Hopper",
        flight="OA202",
        type="damaged",
        tier="gold",
        note="Handle cracked",
    )

    claim = claims.get_claim(conn, claim_id)
    assert claim["passenger"] == "Grace Hopper"
    assert claim["flight"] == "OA202"
    assert claim["type"] == "damaged"
    assert claim["tier"] == "gold"
    assert claim["note"] == "Handle cracked"
    assert claim["status"] == "open"
    assert claim["agent"] is None
    assert claim["filed_at"] == "2026-11-02T14:00:00"

    history = claims.get_history(conn, claim_id)
    assert len(history) == 1
    assert history[0]["claim_id"] == claim_id
    assert history[0]["old_status"] is None
    assert history[0]["new_status"] == "open"
    assert history[0]["actor"] == "desk"
    assert history[0]["changed_at"] == "2026-11-02T14:00:00"


# 2. Invalid input is rejected with a clear error.
@pytest.mark.parametrize(
    ("overrides", "field", "allowed"),
    [
        ({"passenger": ""}, "passenger", []),
        ({"passenger": "   "}, "passenger", []),
        ({"flight": ""}, "flight", []),
        ({"flight": "\t "}, "flight", []),
        ({"type": "stolen"}, "type", ["delayed", "lost", "damaged"]),
        ({"tier": "platinum"}, "tier", ["standard", "silver", "gold"]),
    ],
)
def test_invalid_input_is_rejected_with_clear_error(conn, overrides, field, allowed):
    with pytest.raises(ValueError) as excinfo:
        file_one(conn, **overrides)

    message = str(excinfo.value)
    assert field in message
    for value in allowed:
        assert value in message
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


# 3. The deadline matches the tier.
@pytest.mark.parametrize(
    ("tier", "window"),
    [
        ("gold", timedelta(hours=4)),
        ("silver", timedelta(hours=24)),
        ("standard", timedelta(hours=72)),
    ],
)
def test_deadline_matches_tier(conn, tier, window):
    claim_id = file_one(conn, tier=tier)

    claim = claims.get_claim(conn, claim_id)
    assert claim["filed_at"] == NOW.isoformat()
    assert claim["deadline"] == (NOW + window).isoformat()


# 4. Database constraints reject invalid direct SQL inserts.
@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "stolen"},
        {"tier": "platinum"},
        {"status": "pending"},
    ],
)
def test_check_constraints_reject_bad_claim_rows(conn, overrides):
    with pytest.raises(sqlite3.IntegrityError):
        insert_claim_sql(conn, **overrides)


def test_foreign_key_rejects_history_for_unknown_claim(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO history (claim_id, old_status, new_status, actor, changed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (999, None, "open", "desk", "2026-11-02T14:00:00"),
        )


# 5. list filters claims.
def test_list_filters_by_status_tier_and_both(conn):
    open_gold = file_one(conn, tier="gold")
    assigned_gold = file_one(conn, tier="gold")
    open_silver = file_one(conn, tier="silver")
    conn.execute("UPDATE claims SET status = 'assigned' WHERE id = ?", (assigned_gold,))
    conn.commit()

    def ids(**filters):
        return sorted(row["id"] for row in claims.list_claims(conn, **filters))

    assert ids() == sorted([open_gold, assigned_gold, open_silver])
    assert ids(status="open") == sorted([open_gold, open_silver])
    assert ids(tier="gold") == sorted([open_gold, assigned_gold])
    assert ids(status="open", tier="gold") == [open_gold]
    assert ids(status="closed") == []
