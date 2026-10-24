"""Append-only audit trail and double-entry ledger, in the escrow's SQLite
file and written in the SAME transaction as the state change they record,
so the work queue (sales) and the history can never disagree.

  sale_events  every state transition: when, from what, to what, why, and
               who made it (system | api | operator:<name>)
  ledger       every msat g2c moves, as double entry: the lines of one
               journal entry sum to zero (debit +, credit -)

Both tables are append-only (triggers refuse UPDATE and DELETE; a mistake
is corrected by a new row, never an edit) and hash-chained: each row stores
the hash of the previous one, so editing history breaks verify(). Copy the
heads from verify() off-site daily: that also exposes a rewritten tail.

Personal data (payout targets, player ids) stays out of both tables. It
lives in `sales`, where it can be erased; these rows refer to sale_id only.

Accounts:
  assets:lightning   g2c's balance at its node, as moved by sales
  clearing:sales     seller paid, buyer payment not yet claimed: the money
                     g2c is fronting right now (0 once a sale settles)
  revenue:fees       g2c's fee on every settled sale
  expense:routing    network fee paid to reach the seller
  expense:losses     seller paid but the buyer payment was lost (LOSS)
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable, Optional

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS sale_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,              -- UTC, ISO 8601
  sale_id     TEXT NOT NULL,
  from_state  TEXT,                       -- NULL for the creating event
  to_state    TEXT NOT NULL,
  reason      TEXT,
  actor       TEXT NOT NULL,              -- system | api | operator:<name>
  data        TEXT NOT NULL,              -- JSON facts; never personal data
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sale_events_sale ON sale_events(sale_id);

CREATE TABLE IF NOT EXISTS ledger (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  entry       TEXT NOT NULL,              -- journal entry: <sale_id>:<what>
  sale_id     TEXT,
  account     TEXT NOT NULL,
  amount_msat INTEGER NOT NULL,           -- debit +, credit -
  memo        TEXT NOT NULL,
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_entry ON ledger(entry);
CREATE INDEX IF NOT EXISTS ledger_at ON ledger(at);

CREATE TRIGGER IF NOT EXISTS sale_events_no_update BEFORE UPDATE ON sale_events
BEGIN SELECT RAISE(ABORT, 'sale_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS sale_events_no_delete BEFORE DELETE ON sale_events
BEGIN SELECT RAISE(ABORT, 'sale_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""

EVENT_FIELDS = ("at", "sale_id", "from_state", "to_state", "reason", "actor", "data")
LEDGER_FIELDS = ("at", "entry", "sale_id", "account", "amount_msat", "memo")
CHAINS = (("sale_events", EVENT_FIELDS), ("ledger", LEDGER_FIELDS))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _digest(prev: str, row, fields: tuple) -> str:
    payload = json.dumps([prev] + [row[f] for f in fields],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _append(conn, table: str, fields: tuple, row: dict) -> None:
    """caller holds a write transaction (BEGIN IMMEDIATE): the chain head
    can't move between reading it and appending"""
    last = conn.execute(f"SELECT hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    prev = last[0] if last else GENESIS
    cols = fields + ("prev_hash", "hash")
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) "
                 f"VALUES ({','.join('?' * len(cols))})",
                 [row[f] for f in fields] + [prev, _digest(prev, row, fields)])


def record_event(conn, sale_id: str, from_state: Optional[str], to_state: str,
                 reason: Optional[str] = None, actor: str = "system",
                 data: Optional[dict] = None, at: Optional[str] = None) -> None:
    _append(conn, "sale_events", EVENT_FIELDS, {
        "at": at or now(), "sale_id": sale_id, "from_state": from_state,
        "to_state": to_state, "reason": reason, "actor": actor,
        "data": json.dumps(data or {}, sort_keys=True, separators=(",", ":"))})


def book(conn, entry: str, sale_id: Optional[str], memo: str,
         lines: Iterable[tuple[str, int]], at: Optional[str] = None) -> bool:
    """Book one journal entry, at most once: entry ids are deterministic
    (<sale_id>:<what>), so a retried transition can't book it twice."""
    lines = [(acct, int(amt)) for acct, amt in lines if amt]
    if sum(amt for _, amt in lines) != 0:
        raise ValueError(f"unbalanced entry {entry}: {lines}")
    if conn.execute("SELECT 1 FROM ledger WHERE entry=? LIMIT 1", (entry,)).fetchone():
        return False
    at = at or now()
    for acct, amt in lines:
        _append(conn, "ledger", LEDGER_FIELDS, {
            "at": at, "entry": entry, "sale_id": sale_id, "account": acct,
            "amount_msat": amt, "memo": memo})
    return True


def verify(conn, anchors: Optional[dict] = None) -> dict:
    """Recompute both hash chains and check every entry balances. `heads`
    are what to anchor off-site; pass earlier anchors ({table: hash}) to
    check they are still part of the chain (a rewritten tail drops them)."""
    anchors = anchors or {}
    out = {"ok": True, "heads": {}}
    for table, fields in CHAINS:
        prev, rows, bad, seen = GENESIS, 0, None, False
        for r in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
            if r["prev_hash"] != prev or _digest(prev, r, fields) != r["hash"]:
                bad = r["id"]
                break
            prev, rows = r["hash"], rows + 1
            seen |= r["hash"] == anchors.get(table)
        out[table] = {"rows": rows, "first_bad_id": bad}
        out["ok"] &= bad is None
        if table in anchors:
            out[table]["anchor_found"] = seen
            out["ok"] &= seen
        out["heads"][table] = prev
    unbalanced = [r[0] for r in conn.execute(
        "SELECT entry FROM ledger GROUP BY entry HAVING SUM(amount_msat) != 0")]
    out["unbalanced_entries"] = unbalanced
    out["ok"] &= not unbalanced
    return out


def balances(conn, until: Optional[str] = None) -> dict:
    """account -> balance in msat, up to and including day `until` (UTC)"""
    q, args = "SELECT account, SUM(amount_msat) s FROM ledger", ()
    if until:
        q, args = q + " WHERE substr(at, 1, 10) <= ?", (until,)
    return {r["account"]: r["s"] for r in conn.execute(q + " GROUP BY account", args)}


OUTCOMES = {"COMPLETED": "completed", "CANCELLED": "cancelled", "ERROR": "errors"}
# account -> (report column, sign that makes it positive)
COLUMNS = {"revenue:fees": ("fees_msat", -1), "expense:routing": ("routing_msat", 1),
           "expense:losses": ("losses_msat", 1)}


def summary(conn, start: str, end: str) -> dict:
    """Per UTC day between start and end (YYYY-MM-DD, inclusive): outcomes,
    volume, fees, costs and net; plus balances and exposure at `end`."""
    days: dict[str, dict] = {}

    def day(d):
        return days.setdefault(d, dict.fromkeys(
            [*OUTCOMES.values(), "volume_msat", *(c for c, _ in COLUMNS.values())], 0))

    for r in conn.execute(
            "SELECT substr(at, 1, 10) d, to_state, COUNT(*) n FROM sale_events "
            "WHERE to_state IN ('COMPLETED', 'CANCELLED', 'ERROR') "
            "AND substr(at, 1, 10) BETWEEN ? AND ? GROUP BY d, to_state", (start, end)):
        day(r["d"])[OUTCOMES[r["to_state"]]] = r["n"]
    # volume = what buyers paid in: the only money entering assets:lightning
    for r in conn.execute(
            "SELECT substr(at, 1, 10) d, account, SUM(amount_msat) s, "
            "SUM(MAX(amount_msat, 0)) received FROM ledger "
            "WHERE substr(at, 1, 10) BETWEEN ? AND ? GROUP BY d, account", (start, end)):
        if r["account"] == "assets:lightning":
            day(r["d"])["volume_msat"] = r["received"]
        elif r["account"] in COLUMNS:
            col, sign = COLUMNS[r["account"]]
            day(r["d"])[col] = sign * r["s"]
    rows = [{"day": d, **v, "net_msat": v["fees_msat"] - v["routing_msat"] - v["losses_msat"]}
            for d, v in sorted(days.items())]
    totals = {k: sum(r[k] for r in rows) for k in rows[0] if k != "day"} if rows else {}
    bal = balances(conn, end)
    return {"start": start, "end": end, "days": rows, "totals": totals,
            "balances_msat": bal,
            "open_exposure_msat": bal.get("clearing:sales", 0)}
