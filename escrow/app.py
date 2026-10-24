"""g2c escrow service — hold-invoice escrow over Lightning (LND backend).

API surface (identical to escrow-stub, so the game swaps by ESCROW_URL):
  POST /v1/sales            -> 201 {sale_id, payment_request, payment_hash,
                                    expires_at, fee_msat, seller_amount_msat}
  GET  /v1/sales/{sale_id}  -> {sale_id, payment_hash, state, failure_reason}
  POST /v1/sales/{id}/pay   -> lab demo bridge only (LAB_MODE=1 + DEMO_BRIDGE);
                               on production the buyer's real wallet pays the
                               QR and the HTLC's arrival is the trigger
  POST /v1/sales/{id}/fail  -> lab only: force the seller-payout leg to fail

Operator API (X-Admin-Token, one token per named operator in ADMIN_TOKENS;
disabled when unset):
  GET  /v1/admin/sales/{id}           sale + full history + ledger lines
  POST /v1/admin/sales/{id}/resolve   close an ERROR sale with a note
  GET  /v1/admin/reports/summary      per-day volume, fees, costs, exposure
  GET  /v1/admin/reports/ledger.csv   ledger export for the accountant
  GET  /v1/admin/audit/verify         check the hash chains; heads to anchor

Every state change is recorded, in the same transaction, in an append-only
hash-chained audit trail and a double-entry ledger (ledger.py).

Money flow: hold invoice created HASH-ONLY at the node (LND v2 invoices API —
the node cannot settle without our preimage) -> HTLC arrival detected ->
game RESERVES the item (frozen at the seller, idempotent on payment_hash) ->
seller payout (PayoutResolver: Lightning address; resolved invoice persisted BEFORE
paying, recovered by payment hash after crashes) -> settle -> game COMMITS
the item to the buyer. Any failure before the payout: release + cancel =
item unfrozen, buyer auto-refunded by Lightning itself. Nothing is ever
undone: an item only moves once, after the money is final.

Commit comes AFTER settle on purpose: once the seller is paid the money is
locked in while the buyer's HTLC is still valid, and commit (a promise on a
frozen item) can then be retried for as long as the game is down.

Money-safety invariants (each one closes a way to pay the seller AND refund
the buyer):
  1. Never release or refund while the seller payment is in flight or complete —
     only a payment the node reports FAILED (or never initiated) unlocks
     RELEASING. "Timed out" is not "failed".
  2. Never start a payout unless the buyer's HTLC is still ACCEPTED with
     enough blocks left, and cap the payout route's timelock below the
     buyer HTLC's expiry (like a forwarding node), so the payout resolves
     before LND could auto-cancel the incoming side.
  3. After a settle error, ask the node what happened; never infer COMPLETED
     from error text.
  4. Release the item BEFORE refunding, and only refund once the game
     confirmed the release (a reserved item must never stay frozen for a
     refunded buyer, nor be committed to one).

Crash recovery: every non-terminal sale is re-driven by the reconcile loop;
all steps idempotent. Each sale is driven in its own task, so a slow leg
(LNURL, LND pay, a down game) never stalls the API or other sales.
One process only: a lock file refuses a second instance on the same DB.

LAB_MODE=1 loads lab.py (regtest helpers: demo pay bridge, CLN buyer/seller
stand-ins, peer re-healing). Production never sets it.
"""
import asyncio
import csv
import fcntl
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import ledger
from node import make_node, NodeError
from payout import PayoutResolver, PayoutError

GAME_URL = os.environ.get("GAME_URL", "http://localhost:8000")
SHARED_SECRET = os.environ.get("ESCROW_SHARED_SECRET", "dev-secret")
# fee = FEE_MSAT + amount * FEE_PPM / 1e6 (our revenue, kept as the spread)
FEE_MSAT = int(os.environ.get("FEE_MSAT", "100"))
FEE_PPM = int(os.environ.get("FEE_PPM", "0"))
# routing fee we may spend paying the seller; default = the sale's fee, so a
# sale can never cost more to route than it earns
MAX_ROUTING_FEE_MSAT = os.environ.get("MAX_ROUTING_FEE_MSAT")
INVOICE_EXPIRY_S = int(os.environ.get("INVOICE_EXPIRY_S", "600"))
# final CLTV delta requested from the buyer's HTLC = our time budget (blocks)
HOLD_CLTV_EXPIRY = int(os.environ.get("HOLD_CLTV_EXPIRY", "288"))
# blocks kept between the payout route's timelock and the buyer HTLC's
# expiry (> LND's hold-expiry auto-cancel delta)
HOLD_SAFETY_BLOCKS = int(os.environ.get("HOLD_SAFETY_BLOCKS", "24"))
# below this many usable blocks we refund instead of starting a payout
MIN_PAYOUT_CLTV = int(os.environ.get("MIN_PAYOUT_CLTV", "60"))
GAME_RETRIES = int(os.environ.get("GAME_RETRIES", "5"))
PAYOUT_RETRIES = int(os.environ.get("PAYOUT_RETRIES", "3"))
PAYOUT_TIMEOUT_S = int(os.environ.get("PAYOUT_TIMEOUT_S", "15"))
LOOP_INTERVAL = float(os.environ.get("LOOP_INTERVAL", "0.5"))
NOTIFY_BACKOFF_S = float(os.environ.get("NOTIFY_BACKOFF_S", "10"))
DEMO_BRIDGE = os.environ.get("DEMO_BRIDGE", "1") == "1"   # 0 on signet: real
#                                                          wallets pay the QR,
#                                                          the tap does nothing

LAB_MODE = os.environ.get("LAB_MODE", "0") == "1"
if not LAB_MODE and SHARED_SECRET in ("", "dev-secret"):
    raise SystemExit("ESCROW_SHARED_SECRET must be set to a real secret "
                     "outside LAB_MODE")

# operators: ADMIN_TOKENS="alice:<token>,bob:<token>". One token per person,
# so every admin action in the audit trail names who did it. Unset = the
# operator API is off.
def _admin_tokens(spec: str) -> dict[str, str]:
    """token -> operator name"""
    out = {}
    for pair in filter(None, (p.strip() for p in spec.split(","))):
        name, _, tok = pair.partition(":")
        if not re.fullmatch(r"[a-z0-9_.-]{1,32}", name) or not tok:
            raise SystemExit(f"ADMIN_TOKENS: bad entry for {name!r} (want name:token)")
        if not LAB_MODE and len(tok) < 32:
            raise SystemExit(f"ADMIN_TOKENS: token for {name} is shorter than 32 chars")
        out[tok] = name
    return out


ADMIN_TOKENS = _admin_tokens(os.environ.get("ADMIN_TOKENS", ""))

# the escrow's own node (NODE_BACKEND=cln for lab flavors, lnd on the VPS)
ESCROW_NODE = make_node(require_macaroon=True)

if LAB_MODE:
    import lab                                  # regtest helpers only
    RESOLVER = PayoutResolver(seller_node=lab.SELLER_NODE, escrow_node=ESCROW_NODE)
else:
    RESOLVER = PayoutResolver(seller_node=None, escrow_node=ESCROW_NODE)

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "escrow.db"))

app = FastAPI(title="g2c escrow")

# ---------------------------------------------------------------- state

NON_TERMINAL = ("AWAITING_PAYMENT", "HTLC_HELD", "RESERVING", "PAYING_SELLER",
                "SETTLING", "COMMITTING", "RELEASING", "CANCELLING")
# ERROR is terminal and needs a human: failure_reason says why. An operator
# closes it as RESOLVED, with a note in the audit trail.
# Finished sales drop the payout target (a Lightning address is personal
# data). ERROR keeps it: a human may need it.
FORGET_PAYOUT_IN = ("COMPLETED", "CANCELLED", "RESOLVED")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    """Read or autocommit connection, closed on exit (sqlite3's own context
    manager only commits, it never closes)."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def tx():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sales (
              sale_id            TEXT PRIMARY KEY,
              payment_hash       TEXT UNIQUE NOT NULL,
              preimage           TEXT NOT NULL,          -- our secret until settle
              item_id            INTEGER NOT NULL,
              buyer_id           INTEGER NOT NULL,
              seller_id          INTEGER NOT NULL,
              amount_msat        INTEGER NOT NULL,
              fee_msat           INTEGER NOT NULL,
              seller_amount_msat INTEGER NOT NULL,
              payment_request    TEXT NOT NULL,
              seller_payout_type TEXT NOT NULL DEFAULT 'stub',
              seller_payout_tgt  TEXT,
              seller_bolt11      TEXT,                   -- resolved payout invoice
              seller_pay_hash    TEXT,                   -- payout leg tracking
              force_fail         INTEGER NOT NULL DEFAULT 0,
              payout_attempts    INTEGER NOT NULL DEFAULT 0,
              notify_result      TEXT,                   -- pending game webhook
              routing_fee_msat   INTEGER,                -- actual, once paid
              fiat_currency      TEXT,                   -- price the buyer saw
              fiat_amount        TEXT,                   --   (decimal string)
              state              TEXT NOT NULL,
              failure_reason     TEXT,
              created_at         TEXT NOT NULL,
              updated_at         TEXT NOT NULL,
              expires_at         TEXT NOT NULL,
              payout_forgotten_at TEXT
            );
            """ + ledger.SCHEMA
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sales)")}
    # no migrations by design (yet): a file from an older build is refused
    # rather than half-used
    if not {"updated_at", "routing_fee_msat", "payout_forgotten_at"} <= cols:
        raise SystemExit(f"{DB_PATH} was created by an older escrow build: "
                         "drain it, archive it, and start with a fresh file")
    print(f"[escrow] db ready at {DB_PATH} (game={GAME_URL})")


_LOCK_FILE = None


def acquire_instance_lock() -> None:
    """Two escrow processes on one DB would drive the same sales twice
    (uvicorn --workers N, a stray manual run): refuse to start instead."""
    global _LOCK_FILE
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE = open(str(DB_PATH) + ".lock", "w")
    try:
        fcntl.flock(_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another escrow instance holds {DB_PATH}.lock")


def set_state(sale_id: str, state: str, reason: Optional[str] = None, *,
              actor: str = "system", data: Optional[dict] = None,
              entry: Optional[tuple] = None, fields: Optional[dict] = None) -> None:
    """One state change, its audit event and (at a money moment) its ledger
    entry, in ONE transaction: they land together or not at all.
    entry = (name, memo, [(account, msat), ...]); fields = more sales columns."""
    at = ledger.now()
    fields = dict(fields or {}, state=state, updated_at=at)
    if reason is not None:
        fields["failure_reason"] = reason
    if state in FORGET_PAYOUT_IN:
        fields.update(seller_payout_tgt=None, payout_forgotten_at=at)
    with tx() as conn:
        row = conn.execute("SELECT state FROM sales WHERE sale_id=?",
                           (sale_id,)).fetchone()
        conn.execute(f"UPDATE sales SET {', '.join(f'{k}=?' for k in fields)} "
                     "WHERE sale_id=?", (*fields.values(), sale_id))
        ledger.record_event(conn, sale_id, row["state"] if row else None, state,
                            reason, actor, data, at)
        if entry:
            name, memo, lines = entry
            ledger.book(conn, f"{sale_id}:{name}", sale_id, memo, lines, at)
    print(f"[escrow] {sale_id} -> {state}" + (f" ({reason})" if reason else ""))


def finish(sale_id: str, state: str, reason: Optional[str] = None,
           notify: Optional[str] = None, **kw) -> None:
    """Terminal transition; the game webhook is queued and retried by the
    loop until the game acknowledges it (the game unlocks items on it)."""
    set_state(sale_id, state, reason, fields={"notify_result": notify}, **kw)


def load(sale_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM sales WHERE sale_id=?", (sale_id,)).fetchone()
    return dict(row) if row else None


def save_field(sale_id: str, field: str, value) -> None:
    with tx() as conn:
        conn.execute(f"UPDATE sales SET {field}=? WHERE sale_id=?", (value, sale_id))


def sale_fee_msat(amount_msat: int) -> int:
    return FEE_MSAT + amount_msat * FEE_PPM // 1_000_000


def routing_fee_limit(sale: dict) -> int:
    if MAX_ROUTING_FEE_MSAT is not None:
        return int(MAX_ROUTING_FEE_MSAT)
    return sale["fee_msat"]


# ---------------------------------------------------------------- game api

def game_headers() -> dict:
    return {"X-Escrow-Secret": SHARED_SECRET} if SHARED_SECRET else {}


async def game_call(path: str, body: dict) -> Optional[tuple[int, dict]]:
    """POST to the game contract; returns the first DEFINITIVE answer (200 or
    409) as (status, json), or None when every attempt was transient
    (timeout, 5xx, garbage) — an ambiguous outcome the caller retries."""
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(GAME_RETRIES):
            try:
                r = await client.post(f"{GAME_URL}{path}", headers=game_headers(),
                                      json=body)
                if r.status_code in (200, 409):
                    return r.status_code, r.json()
                print(f"[escrow] {path} {r.status_code}, attempt {attempt+1}")
            except (httpx.HTTPError, ValueError) as e:
                print(f"[escrow] {path} error: {e}")
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def game_reserve(sale: dict) -> Optional[str]:
    """'reserved' (incl. replays) | 'rejected' | None (ambiguous)"""
    res = await game_call("/reserve_sale", {
        "item_id": sale["item_id"], "buyer_id": sale["buyer_id"],
        "seller_id": sale["seller_id"], "idempotency_key": sale["payment_hash"]})
    if res is None:
        return None
    return "reserved" if res[0] == 200 else "rejected"


async def game_commit(sale: dict) -> Optional[str]:
    """'committed' (incl. replays) | 'rejected' | None (ambiguous)"""
    res = await game_call("/commit_sale", {"idempotency_key": sale["payment_hash"]})
    if res is None:
        return None
    return "committed" if res[0] == 200 else "rejected"


async def game_release(sale: dict) -> Optional[str]:
    """'released' (incl. replays and never-reserved) | 'rejected' (the game
    says the sale is committed) | None (ambiguous)"""
    res = await game_call("/release_sale", {
        "idempotency_key": sale["payment_hash"],
        "reason": sale.get("failure_reason") or "release"})
    if res is None:
        return None
    return "released" if res[0] == 200 else "rejected"


async def notify_game(payment_hash: str, result: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(f"{GAME_URL}/internal/sale_result",
                                  headers=game_headers(),
                                  json={"payment_hash": payment_hash, "result": result})
        if r.status_code == 200:
            return True
        print(f"[escrow] webhook {r.status_code}: {r.text[:150]}")
    except httpx.HTTPError as e:
        print(f"[escrow] webhook failed: {e}")
    return False


# ---------------------------------------------------------------- node helpers

def hold_state(payment_hash: str) -> Optional[str]:
    """unified state at our node: open/accepted/settled/cancelled"""
    return ESCROW_NODE.hold_invoice_state(payment_hash)


def htlc_budget(sale: dict) -> tuple[str, Optional[int]]:
    """Is the buyer's HTLC still ours to settle, with time to spare?
    Returns ('ok', cltv_limit) | ('unknown', None) | ('gone'|'late', None).
    cltv_limit None with 'ok' = backend can't tell (lab CLN)."""
    st = hold_state(sale["payment_hash"])
    if st is None:
        return "unknown", None
    if st != "accepted":
        return "gone", None
    try:
        left = ESCROW_NODE.hold_blocks_left(sale["payment_hash"])
    except NodeError:
        return "unknown", None
    if left is None:
        return "ok", None
    usable = left - HOLD_SAFETY_BLOCKS
    if usable < MIN_PAYOUT_CLTV:
        return "late", None
    return "ok", usable


def resolve_payout(sale: dict) -> tuple[str, str]:
    """Resolve the seller's payout invoice once and PERSIST (bolt11, hash)
    BEFORE any payment: retries and crash recovery reuse that exact
    invoice, so the seller can never be paid twice."""
    if sale["seller_pay_hash"] and sale["seller_bolt11"]:
        return sale["seller_bolt11"], sale["seller_pay_hash"]
    bolt11, pay_hash = RESOLVER.resolve(
        {"type": sale["seller_payout_type"],
         "target": sale["seller_payout_tgt"]}, sale)
    with tx() as conn:
        conn.execute(
            "UPDATE sales SET seller_bolt11=?, seller_pay_hash=? "
            "WHERE sale_id=?", (bolt11, pay_hash, sale["sale_id"]))
    return bolt11, pay_hash


def payout_state(sale: dict) -> Optional[str]:
    """complete | pending | failed | None (never initiated). Raises NodeError
    if the node can't be asked — the caller must then wait, not decide."""
    if not sale["seller_pay_hash"]:
        return None
    return ESCROW_NODE.payment_state(sale["seller_pay_hash"])


def payout_fee_msat(sale: dict) -> Optional[int]:
    """actual routing fee of the completed payout; None if the node can't say"""
    try:
        return ESCROW_NODE.payment_fee_msat(sale["seller_pay_hash"])
    except NodeError as e:
        print(f"[escrow] {sale['sale_id']} routing fee unknown: {e}")
        return None


# ---------------------------------------------------------------- bookings
# The two money moments of a sale, each booked in the transaction of the
# state change that follows from it. Between them, clearing:sales holds what
# g2c fronted to the seller and has not yet collected from the buyer.

async def payout_done(sid: str, reason: Optional[str] = None) -> None:
    """seller payment confirmed complete -> SETTLING, booking it"""
    sale = load(sid)                    # fresh: seller_pay_hash may be new
    fee = await asyncio.to_thread(payout_fee_msat, sale)
    paid, cost = sale["seller_amount_msat"], fee or 0
    memo = "seller paid" if fee is not None else "seller paid; routing fee unknown, check the node"
    set_state(sid, "SETTLING", reason, fields={"routing_fee_msat": fee},
              entry=("seller_paid", memo, [("clearing:sales", paid),
                                           ("expense:routing", cost),
                                           ("assets:lightning", -(paid + cost))]),
              data={"seller_pay_hash": sale["seller_pay_hash"],
                    "seller_amount_msat": paid, "routing_fee_msat": fee})


def buyer_settled(sale: dict) -> None:
    """buyer payment claimed -> COMMITTING, booking it. The preimage goes
    in the trail: once settled it is public, and proves the buyer paid."""
    set_state(sale["sale_id"], "COMMITTING",
              entry=("buyer_settled", "buyer payment settled",
                     [("assets:lightning", sale["amount_msat"]),
                      ("clearing:sales", -sale["seller_amount_msat"]),
                      ("revenue:fees", -sale["fee_msat"])]),
              data={"payment_hash": sale["payment_hash"], "preimage": sale["preimage"],
                    "amount_msat": sale["amount_msat"], "fee_msat": sale["fee_msat"]})


# ---------------------------------------------------------------- state machine

async def drive(sale: dict) -> None:
    """advance one non-terminal sale by (at most) one state transition"""
    sid, state = sale["sale_id"], sale["state"]

    if state == "AWAITING_PAYMENT":
        # all node/network calls run in threads: a slow leg (LNURL, LND pay) must never stall the API or other sales
        inv = await asyncio.to_thread(hold_state, sale["payment_hash"])
        exp = datetime.fromisoformat(sale["expires_at"])
        if inv == "accepted":
            set_state(sid, "HTLC_HELD")
        elif inv in ("cancelled", "expired"):
            finish(sid, "CANCELLED", "invoice cancelled/expired at node", "cancelled")
        elif inv in ("settled", "paid"):
            finish(sid, "ERROR", "invoice settled without our preimage?!")
        elif datetime.now(timezone.utc) > exp + timedelta(seconds=60):
            set_state(sid, "CANCELLING", "expired unpaid")
        return

    if state == "HTLC_HELD":
        set_state(sid, "RESERVING")
        return

    if state == "RESERVING":
        verdict, _ = await asyncio.to_thread(htlc_budget, sale)
        if verdict == "unknown":
            return                      # node not answering: decide later
        if verdict != "ok":
            # the buyer's HTLC is gone or about to be: unfreeze the item (a
            # reserve may have landed on an earlier try) and close
            set_state(sid, "RELEASING", f"buyer HTLC {verdict} before reserve")
            return
        outcome = await game_reserve(sale)
        if outcome is None:
            # ambiguous after retries — retried next tick (idempotent);
            # htlc_budget above refunds if the game stays down too long
            return
        if outcome == "rejected":
            set_state(sid, "RELEASING", "reserve_sale rejected")
        else:
            set_state(sid, "PAYING_SELLER")
        return

    if state == "PAYING_SELLER":
        # 1. where is the payout we may already have started?
        ps = await asyncio.to_thread(payout_state, sale)
        if ps == "complete":
            await payout_done(sid)
            return
        if ps == "pending":
            return                      # in flight: wait, never roll back
        if sale["payout_attempts"] >= PAYOUT_RETRIES:
            set_state(sid, "RELEASING", f"seller payment failed x{sale['payout_attempts']}")
            return
        if LAB_MODE and sale["force_fail"]:
            set_state(sid, "RELEASING", "forced failure (debug)")
            return
        # 2. only pay while the buyer's HTLC is still ours, with time to spare
        verdict, cltv_limit = await asyncio.to_thread(htlc_budget, sale)
        if verdict == "unknown":
            return
        if verdict != "ok":
            set_state(sid, "RELEASING", f"buyer HTLC {verdict} before payout")
            return
        # 3. resolve (once) and pay
        try:
            bolt11, _ = await asyncio.to_thread(resolve_payout, sale)
        except PayoutError as e:
            attempts = sale["payout_attempts"] + 1
            save_field(sid, "payout_attempts", attempts)
            print(f"[escrow] {sid} payout resolve attempt {attempts}: {e}")
            if attempts >= PAYOUT_RETRIES:
                set_state(sid, "RELEASING", f"payout resolve failed: {e}")
            return
        save_field(sid, "payout_attempts", sale["payout_attempts"] + 1)
        done, detail = await asyncio.to_thread(
            ESCROW_NODE.pay_invoice, bolt11, PAYOUT_TIMEOUT_S,
            routing_fee_limit(sale), cltv_limit)
        if done:
            await payout_done(sid)
        else:
            # NOT a failure verdict: the next tick asks the node (step 1)
            print(f"[escrow] {sid} payout attempt {sale['payout_attempts'] + 1}: {detail}")
            if LAB_MODE:               # lab peers drop across restarts
                await lab.repeer_seller(ESCROW_NODE)
        return

    if state == "SETTLING":
        # NEVER release after a successful seller payment: settle is ours to keep retrying
        try:
            await asyncio.to_thread(ESCROW_NODE.settle_hold_invoice,
                                    sale["preimage"])
            buyer_settled(sale)
            return
        except NodeError as e:
            err = e
        st = await asyncio.to_thread(hold_state, sale["payment_hash"])
        if st == "settled":
            buyer_settled(sale)
        elif st == "cancelled":
            # seller paid, buyer refunded: the one outcome the design exists
            # to prevent. The item stays reserved (frozen) for a human to
            # decide: commit (buyer keeps a free item) or release.
            lost = sale["seller_amount_msat"]
            finish(sid, "ERROR", f"LOSS: seller paid but buyer HTLC cancelled ({err})",
                   entry=("loss", "seller paid, buyer payment lost",
                          [("expense:losses", lost), ("clearing:sales", -lost)]))
        else:
            print(f"[escrow] {sid} settle error: {err} (invoice {st}, will retry)")
        return

    if state == "COMMITTING":
        # money is final: commit is a promise on a frozen item, retried
        # for as long as the game needs
        outcome = await game_commit(sale)
        if outcome == "committed":
            finish(sid, "COMPLETED", notify="completed")
        elif outcome == "rejected":
            # buyer paid, seller paid, the game won't hand over the item
            # (lost the reservation): a human must deliver or refund
            finish(sid, "ERROR", "PAID, UNDELIVERED: commit_sale rejected")
        return

    if state == "RELEASING":
        # last line of defence for invariant 1, whichever path led here
        ps = await asyncio.to_thread(payout_state, sale)
        if ps == "complete":
            await payout_done(sid, "payout completed after all")
            return
        if ps == "pending":
            return
        outcome = await game_release(sale)
        if outcome == "released":
            set_state(sid, "CANCELLING")
        elif outcome == "rejected":
            # the game says it already committed: the item went to a buyer
            # we never got paid by. Never refund blindly — escalate.
            finish(sid, "ERROR", "release_sale rejected: game reports committed")
        return                          # ambiguous: retry next tick

    if state == "CANCELLING":
        try:
            await asyncio.to_thread(ESCROW_NODE.cancel_hold_invoice,
                                    sale["payment_hash"])
        except NodeError as e:
            print(f"[escrow] {sid} cancel invoice: {e}")
        st = await asyncio.to_thread(hold_state, sale["payment_hash"])
        if st == "cancelled":
            finish(sid, "CANCELLED", notify="cancelled")
        elif st == "settled":
            finish(sid, "ERROR", "invoice settled while cancelling?!")
        # open/accepted/unknown: retry next tick
        return


_running: set[str] = set()
_notify_after: dict[str, float] = {}


async def _drive_one(sale_id: str) -> None:
    try:
        sale = load(sale_id)            # fresh row: no stale snapshot
        if sale and sale["state"] in NON_TERMINAL:
            await drive(sale)
    except Exception as e:              # never let one sale kill the loop
        print(f"[escrow] drive {sale_id} error: {e}")
    finally:
        _running.discard(sale_id)


async def _notify_one(sale_id: str, payment_hash: str, result: str) -> None:
    try:
        if await notify_game(payment_hash, result):
            with tx() as conn:
                conn.execute("UPDATE sales SET notify_result=NULL "
                             "WHERE sale_id=? AND notify_result=?", (sale_id, result))
            _notify_after.pop(sale_id, None)
        else:
            _notify_after[sale_id] = time.monotonic() + NOTIFY_BACKOFF_S
    finally:
        _running.discard("notify:" + sale_id)


async def reconcile_loop() -> None:
    marks = ",".join("?" * len(NON_TERMINAL))
    while True:
        try:
            with db() as conn:
                rows = conn.execute(
                    f"SELECT sale_id FROM sales WHERE state IN ({marks})",
                    NON_TERMINAL).fetchall()
                pending_notes = conn.execute(
                    "SELECT sale_id, payment_hash, notify_result FROM sales "
                    "WHERE notify_result IS NOT NULL").fetchall()
            # one task per sale, at most one in flight per sale
            for row in rows:
                if row["sale_id"] not in _running:
                    _running.add(row["sale_id"])
                    asyncio.create_task(_drive_one(row["sale_id"]))
            now = time.monotonic()
            for row in pending_notes:
                key = "notify:" + row["sale_id"]
                if key not in _running and _notify_after.get(row["sale_id"], 0) <= now:
                    _running.add(key)
                    asyncio.create_task(_notify_one(
                        row["sale_id"], row["payment_hash"], row["notify_result"]))
        except sqlite3.Error as e:
            print(f"[escrow] loop db error: {e}")
        await asyncio.sleep(LOOP_INTERVAL)


@app.on_event("startup")
async def _startup() -> None:
    acquire_instance_lock()
    init_db()
    asyncio.create_task(reconcile_loop())


# ---------------------------------------------------------------- api

class SaleIn(BaseModel):
    item_id: int
    buyer_id: int
    seller_id: int
    amount_msat: int
    seller_payout: dict   # {"type": "lnurl"|"stub" (lab), "target": ...}
    # optional: the price as the buyer saw it ("EUR", "4.99"); recorded so
    # every sale has its fiat value at the time it was made
    fiat_currency: Optional[str] = None
    fiat_amount: Optional[str] = None


def check_secret(x_escrow_secret: str) -> None:
    if SHARED_SECRET and not hmac.compare_digest(
            x_escrow_secret.encode(), SHARED_SECRET.encode()):
        raise HTTPException(401, "bad escrow secret")


def check_sale_in(body: SaleIn) -> tuple[int, str]:
    """(fee, payout type), or 422: refuse now, not after the buyer paid"""
    fee = sale_fee_msat(body.amount_msat)
    if body.amount_msat <= fee:
        raise HTTPException(422, f"amount must exceed fee ({fee} msat)")
    ptype = body.seller_payout.get("type", "stub")
    if ptype not in (("lnurl", "stub", "lab") if LAB_MODE else ("lnurl",)):
        raise HTTPException(422, f"unsupported seller payout type {ptype!r}")
    if ptype == "lnurl" and not body.seller_payout.get("target"):
        raise HTTPException(422, "seller payout target missing")
    if (body.fiat_currency is None) != (body.fiat_amount is None):
        raise HTTPException(422, "fiat_currency and fiat_amount go together")
    if body.fiat_currency is not None:
        try:
            ok = (re.fullmatch(r"[A-Z]{3}", body.fiat_currency)
                  and Decimal(body.fiat_amount).is_finite()
                  and Decimal(body.fiat_amount) > 0)
        except InvalidOperation:
            ok = False
        if not ok:
            raise HTTPException(422, "fiat price must be like EUR / 4.99")
    return fee, ptype


@app.post("/v1/sales")
async def create_sale(body: SaleIn, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    fee, ptype = check_sale_in(body)
    preimage = secrets.token_hex(32)
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    sale_id = secrets.token_hex(6)
    try:
        # hash-only creation: the node cannot settle without our preimage
        bolt11 = await asyncio.to_thread(
            ESCROW_NODE.create_hold_invoice, payment_hash,
            body.amount_msat, INVOICE_EXPIRY_S, HOLD_CLTV_EXPIRY)
    except NodeError as e:
        raise HTTPException(502, f"holdinvoice failed: {e}")
    at = ledger.now()
    facts = {   # what the audit trail records too: no personal data
        "payment_hash": payment_hash, "amount_msat": body.amount_msat,
        "fee_msat": fee, "seller_amount_msat": body.amount_msat - fee,
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(seconds=INVOICE_EXPIRY_S)).isoformat(),
        "fiat_currency": body.fiat_currency, "fiat_amount": body.fiat_amount}
    row = dict(facts, sale_id=sale_id, preimage=preimage, item_id=body.item_id,
               buyer_id=body.buyer_id, seller_id=body.seller_id,
               payment_request=bolt11, seller_payout_type=ptype,
               seller_payout_tgt=body.seller_payout.get("target"),
               state="AWAITING_PAYMENT", created_at=at, updated_at=at)
    with tx() as conn:
        conn.execute(f"INSERT INTO sales ({', '.join(row)}) "
                     f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        ledger.record_event(conn, sale_id, None, "AWAITING_PAYMENT", actor="api",
                            at=at, data=dict(facts, payout_type=ptype))
    return JSONResponse({"sale_id": sale_id, "payment_request": bolt11,
                         **{k: facts[k] for k in ("payment_hash", "expires_at",
                                                  "fee_msat", "seller_amount_msat")}},
                        status_code=201)


@app.get("/v1/sales/{sale_id}")
def get_sale(sale_id: str, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    sale = load(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    return {"sale_id": sale_id, "payment_hash": sale["payment_hash"],
            "state": sale["state"], "failure_reason": sale["failure_reason"]}


@app.post("/v1/sales/{sale_id}/pay")
async def demo_pay(sale_id: str, x_escrow_secret: str = Header(default="")):
    """DEMO BRIDGE: the lab's cln-buyer node pays the invoice. In production
    the buyer's real wallet pays it and the HTLC's arrival is the trigger."""
    check_secret(x_escrow_secret)
    sale = load(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    if sale["state"] != "AWAITING_PAYMENT":
        return {"status": sale["state"]}
    if LAB_MODE and DEMO_BRIDGE:
        await lab.demo_pay(ESCROW_NODE, sale)
        return {"status": "payment submitted"}
    return {"status": "awaiting real wallet payment — scan the QR with "
                      "your Lightning wallet"}


@app.post("/v1/sales/{sale_id}/fail")
def force_fail(sale_id: str, x_escrow_secret: str = Header(default="")):
    if not LAB_MODE:
        raise HTTPException(404, "not found")
    check_secret(x_escrow_secret)
    sale = load(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    save_field(sale_id, "force_fail", 1)
    return {"status": "will_fail"}


@app.get("/v1/metrics")
def metrics():
    """per-state counts + last transitions (JSON; Prometheus later)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) n FROM sales GROUP BY state").fetchall()
        recent = conn.execute(
            "SELECT sale_id, state, failure_reason FROM sales "
            "ORDER BY rowid DESC LIMIT 10").fetchall()
        errors = conn.execute(
            "SELECT sale_id, failure_reason FROM sales WHERE state='ERROR' "
            "ORDER BY rowid DESC LIMIT 20").fetchall()
    return {"states": {r["state"]: r["n"] for r in rows},
            "recent": [dict(r) for r in recent],
            "errors_needing_attention": [dict(r) for r in errors]}


# ---------------------------------------------------------------- operator api

def operator(x_admin_token: str) -> str:
    """the operator's name for this token; every admin action is recorded
    under it"""
    if not ADMIN_TOKENS:
        raise HTTPException(404, "operator API disabled (set ADMIN_TOKENS)")
    found = None
    for tok, name in ADMIN_TOKENS.items():      # check all: constant time
        if hmac.compare_digest(x_admin_token.encode(), tok.encode()):
            found = name
    if not found:
        raise HTTPException(401, "bad admin token")
    return found


def day_range(start: Optional[str], end: Optional[str]) -> tuple[str, str]:
    """inclusive UTC days; default: the current month so far"""
    today = datetime.now(timezone.utc).date()
    try:
        s = date.fromisoformat(start) if start else today.replace(day=1)
        e = date.fromisoformat(end) if end else today
    except ValueError:
        raise HTTPException(422, "dates must be YYYY-MM-DD")
    if s > e:
        raise HTTPException(422, "start is after end")
    return s.isoformat(), e.isoformat()


@app.get("/v1/admin/sales/{sale_id}")
def admin_sale(sale_id: str, x_admin_token: str = Header(default="")):
    """Everything about one sale, for support and disputes."""
    who = operator(x_admin_token)
    sale = load(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    sale.pop("preimage")                # in the trail once settled, never before
    with db() as conn:
        events = conn.execute("SELECT * FROM sale_events WHERE sale_id=? "
                              "ORDER BY id", (sale_id,)).fetchall()
        lines = conn.execute("SELECT * FROM ledger WHERE sale_id=? ORDER BY id",
                             (sale_id,)).fetchall()
    print(f"[escrow] admin {who} viewed sale {sale_id}")
    return {"sale": sale, "events": [dict(r) for r in events],
            "ledger": [dict(r) for r in lines]}


class ResolveIn(BaseModel):
    note: str   # what was decided and done; no personal data


@app.post("/v1/admin/sales/{sale_id}/resolve")
def admin_resolve(sale_id: str, body: ResolveIn, x_admin_token: str = Header(default="")):
    """Close an ERROR sale once a person has handled it (delivered, refunded
    or written off out of band). Moves no money and calls no game: it records
    the decision, by whom, and takes the sale off the attention list."""
    who = operator(x_admin_token)
    sale = load(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    if sale["state"] != "ERROR":
        raise HTTPException(409, f"only ERROR sales can be resolved (is {sale['state']})")
    note = body.note.strip()
    if not 3 <= len(note) <= 2000:
        raise HTTPException(422, "a note of 3 to 2000 characters is required")
    finish(sale_id, "RESOLVED", actor=f"operator:{who}", data={"note": note})
    return {"sale_id": sale_id, "state": "RESOLVED", "by": who}


@app.get("/v1/admin/reports/summary")
def admin_summary(start: Optional[str] = None, end: Optional[str] = None,
                  x_admin_token: str = Header(default="")):
    operator(x_admin_token)
    s, e = day_range(start, end)
    with db() as conn:
        return ledger.summary(conn, s, e)


@app.get("/v1/admin/reports/ledger.csv")
def admin_ledger_csv(start: Optional[str] = None, end: Optional[str] = None,
                     x_admin_token: str = Header(default="")):
    """Every ledger line in the period. fiat_value is the line's share of the
    price the buyer saw, when the game sent one."""
    operator(x_admin_token)
    s, e = day_range(start, end)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "at", "entry", "sale_id", "account", "amount_msat", "memo",
                "fiat_currency", "fiat_value", "hash"])
    with db() as conn:
        for r in conn.execute(
                "SELECT l.*, s.fiat_currency, s.fiat_amount, s.amount_msat sale_msat "
                "FROM ledger l LEFT JOIN sales s USING (sale_id) "
                "WHERE substr(l.at, 1, 10) BETWEEN ? AND ? ORDER BY l.id", (s, e)):
            fiat = ""
            if r["fiat_amount"]:
                fiat = str((Decimal(r["fiat_amount"]) * r["amount_msat"]
                            / r["sale_msat"]).quantize(Decimal("0.0001")))
            w.writerow([r["id"], r["at"], r["entry"], r["sale_id"], r["account"],
                        r["amount_msat"], r["memo"], r["fiat_currency"] or "",
                        fiat, r["hash"]])
    return Response(out.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="g2c-ledger-{s}-{e}.csv"'})


@app.get("/v1/admin/audit/verify")
def admin_verify(events_anchor: Optional[str] = None,
                 ledger_anchor: Optional[str] = None,
                 x_admin_token: str = Header(default="")):
    """Recheck the whole trail. Store `heads` daily somewhere g2c can't
    rewrite; pass the last stored ones back as anchors to prove history
    wasn't rewritten since."""
    operator(x_admin_token)
    anchors = {k: v for k, v in (("sale_events", events_anchor),
                                 ("ledger", ledger_anchor)) if v}
    with db() as conn:
        return ledger.verify(conn, anchors)


@app.get("/v1/health")
def health():
    try:
        ESCROW_NODE.get_info()
        node = "ok"
    except Exception as e:
        node = f"error: {e}"
    return {"ok": node == "ok", "node": node, "game_url": GAME_URL}
