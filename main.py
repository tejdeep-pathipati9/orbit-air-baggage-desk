import argparse
import csv
import sqlite3
import sys
from datetime import datetime

import claims
import db

SEED_HEADER = ["passenger", "flight", "type", "tier", "note"]


def parse_now(value):
    """Parse --now: naive ISO-8601, truncated to whole seconds (no offsets)."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid ISO timestamp {value!r}; expected YYYY-MM-DDTHH:MM:SS"
        ) from None
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError(
            f"{value!r} has a UTC offset; use a local time without an offset"
        )
    return parsed.replace(microsecond=0)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py", description="Orbit Air baggage desk"
    )
    parser.add_argument(
        "--now",
        type=parse_now,
        metavar="ISO",
        help="use this time instead of the clock (YYYY-MM-DDTHH:MM:SS); "
        "must come before the command",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    file_cmd = commands.add_parser("file", help="file a new claim")
    file_cmd.add_argument("--passenger", required=True)
    file_cmd.add_argument("--flight", required=True)
    file_cmd.add_argument("--type", required=True)
    file_cmd.add_argument("--tier", required=True)
    file_cmd.add_argument("--note")
    file_cmd.add_argument("--actor", default="desk")

    list_cmd = commands.add_parser("list", help="list claims")
    list_cmd.add_argument("--status")
    list_cmd.add_argument("--tier")

    assign_cmd = commands.add_parser("assign", help="assign an agent to a claim")
    assign_cmd.add_argument("claim_id", type=int)
    assign_cmd.add_argument("agent")
    assign_cmd.add_argument("--actor", default="desk")

    for name, helptext in (("resolve", "resolve a claim"), ("close", "close a claim")):
        cmd = commands.add_parser(name, help=helptext)
        cmd.add_argument("claim_id", type=int)
        cmd.add_argument("--actor", default="desk")

    commands.add_parser("escalate", help="escalate all overdue claims")

    history_cmd = commands.add_parser("history", help="show a claim's status history")
    history_cmd.add_argument("claim_id", type=int)

    seed_cmd = commands.add_parser("seed", help="import claims from a CSV file")
    seed_cmd.add_argument("path")

    return parser


def cmd_file(conn, args, now):
    claim_id = claims.file_claim(
        conn,
        args.passenger,
        args.flight,
        args.type,
        args.tier,
        args.note,
        now=now,
        actor=args.actor,
    )
    claim = claims.get_claim(conn, claim_id)
    print(f"Filed claim {claim_id} (open, due {claim['deadline']})")


def cmd_list(conn, args, now):
    rows = claims.list_claims(conn, status=args.status, tier=args.tier)
    if not rows:
        print("No claims found.")
        return
    for row in rows:
        line = (
            f"#{row['id']} [{row['status']}] {row['tier']} {row['type']} "
            f"{row['flight']} {row['passenger']} | agent: {row['agent'] or '-'} "
            f"| due {row['deadline']}"
        )
        if row["note"]:
            line += f" | note: {row['note']}"
        print(line)


def cmd_assign(conn, args, now):
    claims.assign_claim(conn, args.claim_id, args.agent, now=now, actor=args.actor)
    print(f"Assigned claim {args.claim_id} to {args.agent}")


def cmd_resolve(conn, args, now):
    claims.resolve_claim(conn, args.claim_id, now=now, actor=args.actor)
    print(f"Resolved claim {args.claim_id}")


def cmd_close(conn, args, now):
    claims.close_claim(conn, args.claim_id, now=now, actor=args.actor)
    print(f"Closed claim {args.claim_id}")


def cmd_escalate(conn, args, now):
    ids = claims.escalate_overdue(conn, now=now)
    if ids:
        print(f"Escalated claims: {', '.join(str(i) for i in ids)}")
    else:
        print("No claims escalated.")


def cmd_history(conn, args, now):
    if claims.get_claim(conn, args.claim_id) is None:
        raise ValueError(f"claim {args.claim_id} not found")
    for row in claims.get_history(conn, args.claim_id):
        print(
            f"{row['changed_at']}  {row['old_status'] or '-'} -> {row['new_status']}"
            f"  by {row['actor']}"
        )


def read_seed_rows(path):
    """Read the seed CSV into row dicts, rejecting any malformed row."""
    try:
        # utf-8-sig also accepts files saved with a byte-order mark (e.g. Excel).
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header != SEED_HEADER:
                raise ValueError(
                    f"seed file header must be exactly: {','.join(SEED_HEADER)}"
                )
            rows = []
            for number, values in enumerate(reader, start=1):
                if not values:  # skip blank lines
                    continue
                if len(values) != len(SEED_HEADER):
                    raise ValueError(
                        f"seed row {number}: expected {len(SEED_HEADER)} columns, "
                        f"found {len(values)} (quote any note that contains a comma)"
                    )
                rows.append(dict(zip(SEED_HEADER, values, strict=True)))
            return rows
    except OSError as exc:
        raise ValueError(f"cannot read seed file {path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"seed file {path} is not valid UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"seed file {path} is not valid CSV: {exc}") from exc


def cmd_seed(conn, args, now):
    ids = claims.seed_claims(conn, read_seed_rows(args.path), now=now)
    print(f"Imported {len(ids)} claims")


COMMANDS = {
    "file": cmd_file,
    "list": cmd_list,
    "assign": cmd_assign,
    "resolve": cmd_resolve,
    "close": cmd_close,
    "escalate": cmd_escalate,
    "history": cmd_history,
    "seed": cmd_seed,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    # The only place the real clock is read; rule code always receives `now`.
    now = args.now or datetime.now().replace(microsecond=0)
    conn = None
    try:
        conn = db.connect()
        db.init_db(conn)
        COMMANDS[args.command](conn, args, now)
    except (ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
