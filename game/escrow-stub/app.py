"""G2C escrow stub — a drop-in stand-in for the real Lightning escrow service.

Honors the same contract as the future real escrow (spec §3.1 from the game's
point of view, §3.2 from ours):

  POST /v1/sales            -> {sale_id, payment_request, payment_hash, expires_at}
  GET  /v1/sales/{sale_id}  -> {state, ...}

and drives the game API exactly like the real state machine will:

  hold "HTLC" (1s) -> reserve_sale (item frozen) -> "pay seller" (1s)
  -> "settle" -> commit_sale (item to buyer) -> webhook /internal/sale_result
  ...or on failure -> release_sale + sale_result cancelled (buyer "refunded").

Swap in the real escrow later by pointing the game's ESCROW_URL elsewhere.
Nothing else changes.

Debug controls:
  POST /v1/sales/{id}/pay     -> simulate the buyer's wallet paying (starts the
                                 state machine; nothing happens until this)
  POST /v1/sales/{id}/fail   -> force this sale down the failure path
  POST /v1/sales/{id}/pause  -> freeze the state machine (simulate stuck)
  POST /v1/sales/{id}/resume -> continue
  SALE_FAIL_EVERY=n env      -> every n-th sale fails automatically
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

GAME_URL = os.environ.get("GAME_URL", "http://localhost:8000")
SHARED_SECRET = os.environ.get("ESCROW_SHARED_SECRET", "dev-secret")
SALE_FAIL_EVERY = int(os.environ.get("SALE_FAIL_EVERY", "0"))
FEE_MSAT = int(os.environ.get("FEE_MSAT", "100"))   # the fee is OUR revenue

# sale_id -> record
SALES: dict[str, dict] = {}
_counter = 0

app = FastAPI(title="g2c escrow stub")

STATES = ["CREATED", "AWAITING_PAYMENT", "HTLC_HELD", "RESERVING",
          "PAYING_SELLER", "SETTLING", "COMMITTING", "COMPLETED",
          "RELEASING", "CANCELLING", "CANCELLED"]


class SaleIn(BaseModel):
    item_id: int
    buyer_id: int
    seller_id: int
    amount_msat: int
    seller_payout: dict  # {type: stub|lnurl, target: ...} — unused here


def _game_headers() -> dict:
    return {"X-Escrow-Secret": SHARED_SECRET} if SHARED_SECRET else {}


def check_secret(x_escrow_secret: str):
    if SHARED_SECRET and x_escrow_secret != SHARED_SECRET:
        raise HTTPException(401, "bad escrow secret")


async def _game_call(method: str, path: str, **kw):
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.request(
            method, f"{GAME_URL}{path}", headers=_game_headers(), **kw)


async def _drive(sale: dict) -> None:
    async def step(state: str, delay: float = 1.0):
        if sale.get("paused"):
            while sale.get("paused"):
                await asyncio.sleep(0.2)
        sale["state"] = state
        await asyncio.sleep(delay)

    key = sale["payment_hash"]
    try:
        await step("HTLC_HELD", 1.0)          # buyer "payment" locked

        await step("RESERVING", 0.2)
        r = await _game_call("POST", "/reserve_sale", json={
            "item_id": sale["req"]["item_id"],
            "buyer_id": sale["req"]["buyer_id"],
            "seller_id": sale["req"]["seller_id"],
            "idempotency_key": key,
        })
        if r.status_code == 409 or sale.get("force_fail"):
            # item unavailable, or (debug) the seller payout "failed":
            # unfreeze the item, then refund the buyer
            await step("RELEASING", 0.5)
            await _game_call("POST", "/release_sale", json={
                "idempotency_key": key, "reason": "sale_failed"})
            await step("CANCELLING", 0.3)
            sale["state"] = "CANCELLED"
            sale["failure_reason"] = ("reserve_sale rejected" if r.status_code == 409
                                      else "forced failure (debug)")
            await _game_call("POST", "/internal/sale_result",
                             json={"payment_hash": key, "result": "cancelled"})
            return

        await step("PAYING_SELLER", 1.0)      # seller invoice paid (X - fee)
        await step("SETTLING", 0.3)           # preimage released: money final
        await step("COMMITTING", 0.2)
        await _game_call("POST", "/commit_sale", json={"idempotency_key": key})
        sale["state"] = "COMPLETED"
        await _game_call("POST", "/internal/sale_result",
                         json={"payment_hash": key, "result": "completed"})
    except Exception as e:  # keep the demo alive; log visibly
        sale["state"] = "ERROR"
        sale["failure_reason"] = f"{type(e).__name__}: {e}"


@app.post("/v1/sales")
async def create_sale(body: SaleIn, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    global _counter
    _counter += 1
    if body.amount_msat <= FEE_MSAT:
        raise HTTPException(422, f"amount must exceed fee ({FEE_MSAT} msat)")
    sale_id = uuid.uuid4().hex[:12]
    payment_hash = uuid.uuid4().hex
    sale = {
        "sale_id": sale_id,
        "payment_hash": payment_hash,
        "payment_request": f"stublnbc_{payment_hash}_{body.amount_msat}msat",
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(seconds=600)).isoformat(),
        "state": "AWAITING_PAYMENT",
        "req": body.model_dump(),
        "force_fail": SALE_FAIL_EVERY > 0 and _counter % SALE_FAIL_EVERY == 0,
        "paused": False,
    }
    SALES[sale_id] = sale
    return JSONResponse({
        "sale_id": sale_id,
        "payment_request": sale["payment_request"],
        "payment_hash": payment_hash,
        "expires_at": sale["expires_at"],
        # the fee is decided by the ESCROW, not the caller:
        "fee_msat": FEE_MSAT,
        "seller_amount_msat": body.amount_msat - FEE_MSAT,
    }, status_code=201)


@app.get("/v1/sales/{sale_id}")
def get_sale(sale_id: str, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    sale = SALES.get(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    return {
        "sale_id": sale_id,
        "payment_hash": sale["payment_hash"],
        "state": sale["state"],
        "failure_reason": sale.get("failure_reason"),
    }


@app.post("/v1/sales/{sale_id}/pay")
async def simulate_pay(sale_id: str, x_escrow_secret: str = Header(default="")):
    """Simulates the buyer's wallet paying the invoice: starts the state machine.
    Nothing happens before this is called."""
    check_secret(x_escrow_secret)
    sale = SALES.get(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    if sale["state"] != "AWAITING_PAYMENT":
        raise HTTPException(409, f"not awaiting payment (state={sale['state']})")
    asyncio.create_task(_drive(sale))
    return {"status": "payment submitted"}


@app.post("/v1/sales/{sale_id}/fail")
def force_fail(sale_id: str, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    sale = SALES.get(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    sale["force_fail"] = True
    return {"status": "will_fail"}


@app.post("/v1/sales/{sale_id}/pause")
def pause(sale_id: str, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    sale = SALES.get(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    sale["paused"] = True
    return {"status": "paused"}


@app.post("/v1/sales/{sale_id}/resume")
def resume(sale_id: str, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    sale = SALES.get(sale_id)
    if not sale:
        raise HTTPException(404, "unknown sale")
    sale["paused"] = False
    return {"status": "resumed"}


@app.get("/v1/health")
def health():
    return {"ok": True, "game_url": GAME_URL}
