"""g2c real escrow service — same contract as the stub, real Lightning inside.

API surface (identical to escrow-stub, so the game swaps by ESCROW_URL):
  POST /v1/sales            -> 201 {sale_id, payment_request, payment_hash,
                                    expires_at, fee_msat, seller_amount_msat}
  GET  /v1/sales/{sale_id}  -> {sale_id, payment_hash, state, failure_reason}
  POST /v1/sales/{id}/pay   -> DEMO bridge: instruct the lab's cln-buyer node
                               to pay the invoice (stands in for the buyer's
                               wallet; the PWA's QR tap reaches this via the
                               game's simulate-pay endpoint)
  POST /v1/sales/{id}/fail  -> debug: force the seller-payout leg to fail

State machine (spec §4), persisted in SQLite, driven by a reconcile loop:

  AWAITING_PAYMENT -> HTLC_HELD -> EXECUTING -> PAYING_SELLER -> SETTLING
  -> COMPLETED
  ...or CANCELLING/ROLLING_BACK -> CANCELLED (buyer auto-refunded by Lightning)

What replaced the stub's theater:
  payment_request  <- real BOLT11 from holdinvoice(hash, msat)   [hash-only!]
  HTLC arrival     <- polling listholdinvoices until state=accepted
  pay seller       <- invoice on the lab's cln-seller node, paid from our node
  settle           <- settleholdinvoice(preimage)
  cancel           <- cancelholdinvoice (refund happens on Lightning)

Crash recovery: every non-terminal sale is re-driven by the same loop on
restart; each step is idempotent (game mutations keyed on payment_hash,
settle/cancel are safe to repeat, seller payout verified via its own
payment hash before settling).
"""
import asyncio
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

from cln import CLN, CLNError
from payout import PayoutResolver, PayoutError

GAME_URL = os.environ.get("GAME_URL", "http://localhost:8000")
SHARED_SECRET = os.environ.get("ESCROW_SHARED_SECRET", "dev-secret")
FEE_MSAT = int(os.environ.get("FEE_MSAT", "100"))
INVOICE_EXPIRY_S = int(os.environ.get("INVOICE_EXPIRY_S", "600"))
EXECUTE_RETRIES = int(os.environ.get("EXECUTE_RETRIES", "5"))
PAYOUT_RETRIES = int(os.environ.get("PAYOUT_RETRIES", "3"))
LOOP_INTERVAL = float(os.environ.get("LOOP_INTERVAL", "0.5"))
DEMO_BRIDGE = os.environ.get("DEMO_BRIDGE", "1") == "1"   # 0 on signet: real
#                                                          wallets pay the QR,
#                                                          the tap does nothing

CLN_NETWORK = os.environ.get("CLN_NETWORK", "regtest")  # signet on the VPS
ESCROW_NODE = CLN(os.environ.get("ESCROW_NODE_DIR", "/sockets/escrow"), CLN_NETWORK)
BUYER_NODE = CLN(os.environ.get("BUYER_NODE_DIR", "/sockets/buyer"), CLN_NETWORK)
SELLER_NODE = CLN(os.environ.get("SELLER_NODE_DIR", "/sockets/seller"), CLN_NETWORK)

RESOLVER = PayoutResolver(seller_node=SELLER_NODE, escrow_node=ESCROW_NODE)

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "escrow.db"))

app = FastAPI(title="g2c escrow")

# ---------------------------------------------------------------- state

NON_TERMINAL = {"AWAITING_PAYMENT", "HTLC_HELD", "EXECUTING", "PAYING_SELLER",
                "SETTLING", "ROLLING_BACK", "CANCELLING"}


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
                ("payout_attempts", "ALTER TABLE sales ADD COLUMN payout_attempts INTEGER NOT NULL DEFAULT 0")):
            if col not in existing:
                conn.execute(ddl)
    print(f"[escrow] db ready at {DB_PATH} (game={GAME_URL})")


def set_state(sale_id: str, state: str, reason: Optional[str] = None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE sales SET state=?, failure_reason=COALESCE(?, failure_reason) "
            "WHERE sale_id=?", (state, reason, sale_id))
    print(f"[escrow] {sale_id} -> {state}" + (f" ({reason})" if reason else ""))


def load(sale_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM sales WHERE sale_id=?", (sale_id,)).fetchone()
    return dict(row) if row else None


def save_field(sale_id: str, field: str, value) -> None:
    with tx() as conn:
        conn.execute(f"UPDATE sales SET {field}=? WHERE sale_id=?", (value, sale_id))


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


async def game_rollback(sale: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(3):
            try:
                await client.post(f"{GAME_URL}/rollback_sale", headers=game_headers(),
                                  json={"idempotency_key": sale["payment_hash"],
                                        "reason": "seller_payment_failed"})
                return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    # rollback failed — leave state ROLLING_BACK for the reconcile loop to retry


async def notify_game(payment_hash: str, result: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{GAME_URL}/internal/sale_result",
                              headers=game_headers(),
                              json={"payment_hash": payment_hash, "result": result})
    except httpx.HTTPError as e:
        print(f"[escrow] webhook failed: {e}")


# ---------------------------------------------------------------- cln helpers

def hold_state(payment_hash: str) -> Optional[str]:
    """state of a hold invoice at our node: open/unpaid/accepted/settled/cancelled"""
    try:
        res = ESCROW_NODE.call("listholdinvoices", [payment_hash])
    except CLNError:
        return None
    invs = res.get("holdinvoices", [])
    if not invs:
        return None
    return invs[0].get("state")


def cln_pay_ok(node: CLN, bolt11: str, retry_for: int = 30) -> tuple[bool, str]:
    """Pay an invoice from `node`. Returns (done, status)."""
    try:
        res = node.call("pay", {"bolt11": bolt11, "retry_for": retry_for})
        status = res.get("status", "pending")
    except CLNError as e:
        return False, str(e)
    return status == "complete", status


def seller_payout(sale: dict) -> tuple[bool, Optional[str], str]:
    """Resolve + pay the seller payout. Returns (ok, payment_hash, status).

    CRASH-SAFE across all profiles: we resolve (or recover via deterministic
    label) and PERSIST (bolt11, payment_hash) BEFORE paying — a crash after
    paying is recovered via payout_verified() on the recorded hash, never by
    paying a second invoice."""
    if sale["seller_pay_hash"] and sale["seller_bolt11"]:
        bolt11, pay_hash = sale["seller_bolt11"], sale["seller_pay_hash"]
    else:
        try:
            bolt11, pay_hash = RESOLVER.resolve(
                {"type": sale["seller_payout_type"],
                 "target": sale["seller_payout_tgt"]}, sale)
        except PayoutError as e:
            return False, None, f"resolve: {e}"
        with tx() as conn:
            conn.execute(
                "UPDATE sales SET seller_bolt11=?, seller_pay_hash=? "
                "WHERE sale_id=?", (bolt11, pay_hash, sale["sale_id"]))
    ok, status = cln_pay_ok(ESCROW_NODE, bolt11, retry_for=15)
    return ok, pay_hash, status


def payout_verified(sale: dict) -> bool:
    """crash-recovery: was the seller payment already completed?"""
    if not sale["seller_pay_hash"]:
        return False
    try:
        res = ESCROW_NODE.call("listpays", {"payment_hash": sale["seller_pay_hash"]})
        pays = res.get("pays", [])
    except CLNError:
        return False
    return any(p.get("status") == "complete" for p in pays)


# ---------------------------------------------------------------- state machine

async def cancel_and_close(sale: dict, reason: str) -> None:
    set_state(sale["sale_id"], "CANCELLING", reason)
    try:
        ESCROW_NODE.call("cancelholdinvoice", [sale["payment_hash"]])
    except CLNError as e:
        print(f"[escrow] cancelholdinvoice: {e}")
    set_state(sale["sale_id"], "CANCELLED", reason)
    await notify_game(sale["payment_hash"], "cancelled")


async def drive(sale: dict) -> None:
    """advance one non-terminal sale by (at most) one state transition"""
    sid, state = sale["sale_id"], sale["state"]

    if state == "AWAITING_PAYMENT":
        inv = hold_state(sid and sale["payment_hash"])
        exp = datetime.fromisoformat(sale["expires_at"])
        if inv == "accepted":
            set_state(sid, "HTLC_HELD")
        elif inv in ("cancelled", "expired"):
            set_state(sid, "CANCELLED", "invoice cancelled/expired at node")
            await notify_game(sale["payment_hash"], "cancelled")
        elif inv in ("settled", "paid"):
            set_state(sid, "ERROR", "invoice settled without our preimage?!")
        elif datetime.now(timezone.utc) > exp + timedelta(seconds=60):
            await cancel_and_close(sale, "expired unpaid")
        return

    if state == "HTLC_HELD":
        set_state(sid, "EXECUTING")
        return

    if state == "EXECUTING":
        try:
            outcome = await game_execute(sale)
        except ConnectionError:
            # ambiguous after retries — the reconcile loop will retry next tick;
            # buyer's HTLC fails safely on its own if we stall too long
            return
        if outcome == "rejected":
            await cancel_and_close(sale, "execute_sale rejected")
        else:
            set_state(sid, "PAYING_SELLER")
        return

    if state == "PAYING_SELLER":
        if sale["force_fail"]:
            set_state(sid, "ROLLING_BACK", "forced failure (debug)")
            return
        if payout_verified(sale):   # crash-recovery path
            set_state(sid, "SETTLING")
            return
        ok, pay_hash, detail = seller_payout(sale)
        if pay_hash:
            save_field(sid, "seller_pay_hash", pay_hash)
        if ok:
            set_state(sid, "SETTLING")
            return
        attempts = sale["payout_attempts"] + 1
        save_field(sid, "payout_attempts", attempts)
        if attempts >= PAYOUT_RETRIES:
            set_state(sid, "ROLLING_BACK", f"seller payment failed x{attempts}")
        else:
            print(f"[escrow] payout attempt {attempts} not ok: {detail}")
        return

    if state == "SETTLING":
        # NEVER rollback after a successful seller payment: settle is ours to keep retrying
        try:
            ESCROW_NODE.call("settleholdinvoice", [sale["preimage"]])
            set_state(sid, "COMPLETED")
            await notify_game(sale["payment_hash"], "completed")
        except CLNError as e:
            if "already" in str(e).lower() or "paid" in str(e).lower():
                set_state(sid, "COMPLETED")
                await notify_game(sale["payment_hash"], "completed")
            else:
                print(f"[escrow] settleholdinvoice error: {e} (will retry)")
        return

    if state in ("ROLLING_BACK",):
        await game_rollback(sale)
        await cancel_and_close(sale, sale.get("failure_reason") or "rolling back")
        return


async def reconcile_loop() -> None:
    while True:
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM sales WHERE state IN "
                    "('AWAITING_PAYMENT','HTLC_HELD','EXECUTING','PAYING_SELLER',"
                    "'SETTLING','ROLLING_BACK')").fetchall()
            for row in rows:
                try:
                    await drive(dict(row))
                except Exception as e:   # never let one sale kill the loop
                    print(f"[escrow] drive {row['sale_id']} error: {e}")
        except sqlite3.Error as e:
            print(f"[escrow] loop db error: {e}")
        await asyncio.sleep(LOOP_INTERVAL)


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    asyncio.create_task(reconcile_loop())


# ---------------------------------------------------------------- api

class SaleIn(BaseModel):
    item_id: int
    buyer_id: int
    seller_id: int
    amount_msat: int
    seller_payout: dict   # regtest profile: always pays the lab's seller node


def check_secret(x_escrow_secret: str) -> None:
    if SHARED_SECRET and x_escrow_secret != SHARED_SECRET:
        raise HTTPException(401, "bad escrow secret")


@app.post("/v1/sales")
async def create_sale(body: SaleIn, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    if body.amount_msat <= FEE_MSAT:
        raise HTTPException(422, f"amount must exceed fee ({FEE_MSAT} msat)")
    preimage = secrets.token_hex(32)
    import hashlib
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    sale_id = secrets.token_hex(6)
    try:
        # hash-only creation: the node cannot settle without our preimage
        inv = ESCROW_NODE.call("holdinvoice", [payment_hash, body.amount_msat])
    except CLNError as e:
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
             body.seller_id, body.amount_msat, FEE_MSAT,
             body.amount_msat - FEE_MSAT, inv["bolt11"],
             body.seller_payout.get("type", "stub"),
             body.seller_payout.get("target"),
             "AWAITING_PAYMENT", datetime.now(timezone.utc).isoformat(),
             expires_at))
    return JSONResponse({
        "sale_id": sale_id,
        "payment_request": inv["bolt11"],
        "payment_hash": payment_hash,
        "expires_at": expires_at,
        "fee_msat": FEE_MSAT,
        "seller_amount_msat": body.amount_msat - FEE_MSAT,
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
    if not DEMO_BRIDGE:
        return {"status": "awaiting real wallet payment — scan the QR with "
                          "your Lightning wallet (signet profile)"}

    # fire-and-forget: the buyer-node payment runs in the background; the
    # reconcile loop detects the arriving HTLC on its own, so the HTTP
    # response doesn't need to wait for `pay` (which blocks ~10s while the
    # HTLC is held). Guarded against double-fire by state check above.
    async def _bg_pay():
        for attempt in range(3):
            try:
                _, status = await asyncio.to_thread(
                    cln_pay_ok, BUYER_NODE, sale["payment_request"], 8)
            except Exception:
                status = "error"
            if "Unknown" not in status and "Could not find" not in status:
                return
            print(f"[escrow] buyer pay retry {attempt+1}: {status}")
            await asyncio.sleep(5)

    asyncio.create_task(_bg_pay())
    return {"status": "payment submitted"}


@app.post("/v1/sales/{sale_id}/fail")
def force_fail(sale_id: str, x_escrow_secret: str = Header(default="")):
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
    return {"states": {r["state"]: r["n"] for r in rows},
            "recent": [dict(r) for r in recent]}


@app.get("/v1/health")
def health():
    try:
        ESCROW_NODE.call("getinfo")
        node = "ok"
    except Exception as e:
        node = f"error: {e}"
    return {"ok": node == "ok", "node": node, "game_url": GAME_URL}
