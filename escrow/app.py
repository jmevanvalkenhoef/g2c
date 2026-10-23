"""g2c escrow service — hold-invoice escrow over Lightning (LND backend).

API surface (identical to escrow-stub, so the game swaps by ESCROW_URL):
  POST /v1/sales            -> 201 {sale_id, payment_request, payment_hash,
                                    expires_at, fee_msat, seller_amount_msat}
  GET  /v1/sales/{sale_id}  -> {sale_id, payment_hash, state, failure_reason}
  POST /v1/sales/{id}/pay   -> lab demo bridge only (LAB_MODE=1 + DEMO_BRIDGE);
                               on production the buyer's real wallet pays the
                               QR and the HTLC's arrival is the trigger
  POST /v1/sales/{id}/fail  -> lab only: force the seller-payout leg to fail

Money flow: hold invoice created HASH-ONLY at the node (LND v2 invoices API —
the node cannot settle without our preimage) -> HTLC arrival detected ->
item transfers at the game (idempotent, keyed on payment_hash) -> seller
payout (PayoutResolver: lnurl/nwc; resolved invoice persisted BEFORE paying,
recovered by payment hash after crashes) -> settle. Any failure before the
payout: rollback + cancel = buyer auto-refunded by Lightning itself.

Money-safety invariants (each one closes a way to pay the seller AND refund
the buyer):
  1. Never roll back while the seller payment is in flight or complete —
     only a payment the node reports FAILED (or never initiated) unlocks
     ROLLING_BACK. "Timed out" is not "failed".
  2. Never start a payout unless the buyer's HTLC is still ACCEPTED with
     enough blocks left, and cap the payout route's timelock below the
     buyer HTLC's expiry (like a forwarding node), so the payout resolves
     before LND could auto-cancel the incoming side.
  3. After a settle error, ask the node what happened; never infer COMPLETED
     from error text.
  4. Roll back the item BEFORE refunding, and only refund once the game
     confirmed the rollback.

Crash recovery: every non-terminal sale is re-driven by the reconcile loop;
all steps idempotent. Each sale is driven in its own task, so a slow leg
(LNURL, NWC relay, LND pay, a down game) never stalls the API or other sales.
One process only: a lock file refuses a second instance on the same DB.

LAB_MODE=1 loads lab.py (regtest helpers: demo pay bridge, CLN buyer/seller
stand-ins, peer re-healing). Production never sets it.
"""
import asyncio
import fcntl
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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
EXECUTE_RETRIES = int(os.environ.get("EXECUTE_RETRIES", "5"))
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

NON_TERMINAL = ("AWAITING_PAYMENT", "HTLC_HELD", "EXECUTING", "PAYING_SELLER",
                "SETTLING", "ROLLING_BACK", "CANCELLING")
# ERROR is terminal and needs a human: failure_reason says why


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def tx():
    conn = db()
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
              state              TEXT NOT NULL,
              failure_reason     TEXT,
              created_at         TEXT NOT NULL,
              expires_at         TEXT NOT NULL
            );
            """
        )
    # escrow state persists by design (unlike the demo game DB), so guard-add
    # columns introduced after the first deploy
    with db() as conn:
        existing = [r[1] for r in conn.execute("PRAGMA table_info(sales)")]
        for col, ddl in (
                ("seller_payout_type", "ALTER TABLE sales ADD COLUMN seller_payout_type TEXT NOT NULL DEFAULT 'stub'"),
                ("seller_payout_tgt", "ALTER TABLE sales ADD COLUMN seller_payout_tgt TEXT"),
                ("seller_bolt11", "ALTER TABLE sales ADD COLUMN seller_bolt11 TEXT"),
                ("payout_attempts", "ALTER TABLE sales ADD COLUMN payout_attempts INTEGER NOT NULL DEFAULT 0"),
                ("notify_result", "ALTER TABLE sales ADD COLUMN notify_result TEXT")):
            if col not in existing:
                conn.execute(ddl)
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


def set_state(sale_id: str, state: str, reason: Optional[str] = None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE sales SET state=?, failure_reason=COALESCE(?, failure_reason) "
            "WHERE sale_id=?", (state, reason, sale_id))
    print(f"[escrow] {sale_id} -> {state}" + (f" ({reason})" if reason else ""))


def finish(sale_id: str, state: str, reason: Optional[str] = None,
           notify: Optional[str] = None) -> None:
    """Terminal transition; the game webhook is queued and retried by the
    loop until the game acknowledges it (the game unlocks items on it)."""
    with tx() as conn:
        conn.execute(
            "UPDATE sales SET state=?, failure_reason=COALESCE(?, failure_reason), "
            "notify_result=? WHERE sale_id=?", (state, reason, notify, sale_id))
    print(f"[escrow] {sale_id} -> {state}" + (f" ({reason})" if reason else ""))


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


async def game_execute(sale: dict) -> str:
    """returns 'done' | 'already_done' | 'rejected' — raises on transient errors"""
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(EXECUTE_RETRIES):
            try:
                r = await client.post(f"{GAME_URL}/execute_sale", headers=game_headers(),
                                      json={"item_id": sale["item_id"],
                                            "buyer_id": sale["buyer_id"],
                                            "seller_id": sale["seller_id"],
                                            "idempotency_key": sale["payment_hash"]})
                if r.status_code == 200:
                    return r.json().get("status", "done")
                if r.status_code == 409:
                    return "rejected"
                print(f"[escrow] execute_sale {r.status_code}, attempt {attempt+1}")
            except httpx.HTTPError as e:
                print(f"[escrow] execute_sale error: {e}")
            await asyncio.sleep(0.5 * (attempt + 1))
    raise ConnectionError("execute_sale kept failing (ambiguous)")


async def game_rollback(sale: dict) -> bool:
    """True only once the game confirmed the item is back with the seller
    (or was never moved). Refunding before that gives the buyer the item
    for free."""
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(3):
            try:
                r = await client.post(f"{GAME_URL}/rollback_sale", headers=game_headers(),
                                      json={"idempotency_key": sale["payment_hash"],
                                            "reason": sale.get("failure_reason") or "rollback"})
                if r.status_code == 200 and r.json().get("status") in (
                        "rolled_back", "already_rolled_back", "not_found_ignored"):
                    return True
                print(f"[escrow] rollback_sale {r.status_code}: {r.text[:150]}")
            except (httpx.HTTPError, ValueError) as e:
                print(f"[escrow] rollback_sale error: {e}")
            await asyncio.sleep(0.5)
    return False


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


# ---------------------------------------------------------------- state machine

async def drive(sale: dict) -> None:
    """advance one non-terminal sale by (at most) one state transition"""
    sid, state = sale["sale_id"], sale["state"]

    if state == "AWAITING_PAYMENT":
        # all node/network calls run in threads: a slow leg (LNURL, NWC
        # relay, LND pay) must never stall the API or other sales
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
        set_state(sid, "EXECUTING")
        return

    if state == "EXECUTING":
        verdict, _ = await asyncio.to_thread(htlc_budget, sale)
        if verdict == "unknown":
            return                      # node not answering: decide later
        if verdict != "ok":
            # the buyer's HTLC is gone or about to be: the item must not
            # stay transferred (execute may have landed on an earlier try)
            set_state(sid, "ROLLING_BACK", f"buyer HTLC {verdict} before transfer")
            return
        try:
            outcome = await game_execute(sale)
        except ConnectionError:
            # ambiguous after retries — retried next tick (idempotent);
            # htlc_budget above refunds if the game stays down too long
            return
        if outcome == "rejected":
            set_state(sid, "CANCELLING", "execute_sale rejected")
        else:
            set_state(sid, "PAYING_SELLER")
        return

    if state == "PAYING_SELLER":
        # 1. where is the payout we may already have started?
        ps = await asyncio.to_thread(payout_state, sale)
        if ps == "complete":
            set_state(sid, "SETTLING")
            return
        if ps == "pending":
            return                      # in flight: wait, never roll back
        if sale["payout_attempts"] >= PAYOUT_RETRIES:
            set_state(sid, "ROLLING_BACK", f"seller payment failed x{sale['payout_attempts']}")
            return
        if LAB_MODE and sale["force_fail"]:
            set_state(sid, "ROLLING_BACK", "forced failure (debug)")
            return
        # 2. only pay while the buyer's HTLC is still ours, with time to spare
        verdict, cltv_limit = await asyncio.to_thread(htlc_budget, sale)
        if verdict == "unknown":
            return
        if verdict != "ok":
            set_state(sid, "ROLLING_BACK", f"buyer HTLC {verdict} before payout")
            return
        # 3. resolve (once) and pay
        try:
            bolt11, _ = await asyncio.to_thread(resolve_payout, sale)
        except PayoutError as e:
            attempts = sale["payout_attempts"] + 1
            save_field(sid, "payout_attempts", attempts)
            print(f"[escrow] {sid} payout resolve attempt {attempts}: {e}")
            if attempts >= PAYOUT_RETRIES:
                set_state(sid, "ROLLING_BACK", f"payout resolve failed: {e}")
            return
        save_field(sid, "payout_attempts", sale["payout_attempts"] + 1)
        done, detail = await asyncio.to_thread(
            ESCROW_NODE.pay_invoice, bolt11, PAYOUT_TIMEOUT_S,
            routing_fee_limit(sale), cltv_limit)
        if done:
            set_state(sid, "SETTLING")
        else:
            # NOT a failure verdict: the next tick asks the node (step 1)
            print(f"[escrow] {sid} payout attempt {sale['payout_attempts'] + 1}: {detail}")
            if LAB_MODE:               # lab peers drop across restarts
                await lab.repeer_seller(ESCROW_NODE)
        return

    if state == "SETTLING":
        # NEVER rollback after a successful seller payment: settle is ours to keep retrying
        try:
            await asyncio.to_thread(ESCROW_NODE.settle_hold_invoice,
                                    sale["preimage"])
            finish(sid, "COMPLETED", notify="completed")
            return
        except NodeError as e:
            err = e
        st = await asyncio.to_thread(hold_state, sale["payment_hash"])
        if st == "settled":
            finish(sid, "COMPLETED", notify="completed")
        elif st == "cancelled":
            # seller paid, buyer refunded: the one outcome the design exists
            # to prevent. Item stays with the buyer (they did pay, then got
            # refunded); a human has to reconcile.
            finish(sid, "ERROR", f"LOSS: seller paid but buyer HTLC cancelled ({err})",
                   notify="completed")
        else:
            print(f"[escrow] {sid} settle error: {err} (invoice {st}, will retry)")
        return

    if state == "ROLLING_BACK":
        # last line of defence for invariant 1, whichever path led here
        ps = await asyncio.to_thread(payout_state, sale)
        if ps == "complete":
            set_state(sid, "SETTLING", "payout completed after all")
            return
        if ps == "pending":
            return
        if await game_rollback(sale):
            set_state(sid, "CANCELLING")
        return                          # rollback unconfirmed: retry next tick

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
    seller_payout: dict   # {"type": "lnurl"|"nwc"|"stub" (lab), "target": ...}


def check_secret(x_escrow_secret: str) -> None:
    if SHARED_SECRET and not hmac.compare_digest(
            x_escrow_secret.encode(), SHARED_SECRET.encode()):
        raise HTTPException(401, "bad escrow secret")


@app.post("/v1/sales")
async def create_sale(body: SaleIn, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    fee = sale_fee_msat(body.amount_msat)
    if body.amount_msat <= fee:
        raise HTTPException(422, f"amount must exceed fee ({fee} msat)")
    ptype = body.seller_payout.get("type", "stub")
    if ptype not in (("lnurl", "nwc", "stub", "lab") if LAB_MODE else ("lnurl", "nwc")):
        # refuse now, not after the buyer paid
        raise HTTPException(422, f"unsupported seller payout type {ptype!r}")
    if ptype in ("lnurl", "nwc") and not body.seller_payout.get("target"):
        raise HTTPException(422, "seller payout target missing")
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
    expires_at = (datetime.now(timezone.utc)
                  + timedelta(seconds=INVOICE_EXPIRY_S)).isoformat()
    with tx() as conn:
        conn.execute(
            """INSERT INTO sales (sale_id, payment_hash, preimage, item_id,
                 buyer_id, seller_id, amount_msat, fee_msat, seller_amount_msat,
                 payment_request, seller_payout_type, seller_payout_tgt,
                 state, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sale_id, payment_hash, preimage, body.item_id, body.buyer_id,
             body.seller_id, body.amount_msat, fee,
             body.amount_msat - fee, bolt11, ptype,
             body.seller_payout.get("target"),
             "AWAITING_PAYMENT", datetime.now(timezone.utc).isoformat(),
             expires_at))
    return JSONResponse({
        "sale_id": sale_id,
        "payment_request": bolt11,
        "payment_hash": payment_hash,
        "expires_at": expires_at,
        "fee_msat": fee,
        "seller_amount_msat": body.amount_msat - fee,
    }, status_code=201)


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


@app.get("/v1/health")
def health():
    try:
        ESCROW_NODE.get_info()
        node = "ok"
    except Exception as e:
        node = f"error: {e}"
    return {"ok": node == "ok", "node": node, "game_url": GAME_URL}
