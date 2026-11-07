import os
import sqlite3

DEFAULT_DB = "baggage.db"

STATUSES = ("open", "assigned", "escalated", "resolved", "closed")
TYPES = ("delayed", "lost", "damaged")
TIERS = ("standard", "silver", "gold")


def _in_list(values):
    return ", ".join(f"'{value}'" for value in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS claims (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    passenger TEXT NOT NULL CHECK (trim(passenger) <> ''),
    flight    TEXT NOT NULL CHECK (trim(flight) <> ''),
    type      TEXT NOT NULL CHECK (type IN ({_in_list(TYPES)})),
    tier      TEXT NOT NULL CHECK (tier IN ({_in_list(TIERS)})),
    status    TEXT NOT NULL DEFAULT 'open' CHECK (status IN ({_in_list(STATUSES)})),
    agent     TEXT,
    note      TEXT,
    filed_at  TEXT NOT NULL,
    deadline  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id   INTEGER NOT NULL REFERENCES claims(id),
    old_status TEXT CHECK (old_status IS NULL OR old_status IN ({_in_list(STATUSES)})),
    new_status TEXT NOT NULL CHECK (new_status IN ({_in_list(STATUSES)})),
    actor      TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
"""


def connect(path=None):
    """Open a connection with rows as sqlite3.Row and foreign keys enforced."""
    if path is None:
        path = os.environ.get("ORBIT_DB", DEFAULT_DB)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
