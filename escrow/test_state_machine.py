#!/usr/bin/env python3
"""State-machine money-safety tests: a fake node + fake game drive app.drive()
through the failure paths that used to pay the seller AND refund the buyer.

Run: uv run python test_state_machine.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, ".")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "escrow.db")
os.environ["ESCROW_SHARED_SECRET"] = "test-secret"
os.environ["NODE_BACKEND"] = "cln"          # replaced by FakeNode below
os.environ["LAB_MODE"] = "0"
os.environ["FEE_MSAT"] = "1000"
os.environ["FEE_PPM"] = "10000"             # 1 %

import app                                   # noqa: E402
from node import NodeError                   # noqa: E402

PASS = FAIL = 0


def check(label, want, got):
    global PASS, FAIL
    if want == got:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}: want {want!r}, got {got!r}")


class FakeNode:
    def __init__(self):
        self.hold = {}            # payment_hash -> state
        self.blocks_left = 200
        self.payments = {}        # pay_hash -> state
        self.pay_calls = []       # (bolt11, fee_limit, cltv_limit)
        self.pay_result = "complete"     # complete | pending | failed
        self.settle_error = None
        self.cancelled = []
        self.unreachable = False

    def create_hold_invoice(self, h, amount, expiry, cltv=None):
        self.hold[h] = "open"
        return f"lnbc-hold-{h[:8]}"

    def hold_invoice_state(self, h):
        return None if self.unreachable else self.hold.get(h)

    def hold_blocks_left(self, h):
        if self.unreachable:
            raise NodeError("down")
        return self.blocks_left if self.hold.get(h) == "accepted" else None

    def settle_hold_invoice(self, preimage):
        if self.settle_error:
            raise NodeError(self.settle_error)
        import hashlib
        self.hold[hashlib.sha256(bytes.fromhex(preimage)).hexdigest()] = "settled"

    def cancel_hold_invoice(self, h):
        self.cancelled.append(h)
        self.hold[h] = "cancelled"

    def pay_invoice(self, bolt11, timeout, fee_limit, cltv_limit):
        self.pay_calls.append((bolt11, fee_limit, cltv_limit))
        h = "pay-" + bolt11
        self.payments[h] = self.pay_result
        return (self.pay_result == "complete"), self.pay_result

    def payment_state(self, h):
        if self.unreachable:
            raise NodeError("down")
        return self.payments.get(h)

    def get_info(self):
        return {}


class FakeResolver:
    def resolve(self, payout, sale):
        bolt11 = f"seller-{sale['sale_id']}"
        return bolt11, "pay-" + bolt11


class FakeGame:
    def __init__(self):
        self.executed = []
        self.rolled_back = []
        self.rollback_ok = True
        self.execute_outcome = "done"

    async def execute(self, sale):
        if self.execute_outcome == "down":
            raise ConnectionError("down")
        self.executed.append(sale["payment_hash"])
        return self.execute_outcome

    async def rollback(self, sale):
        if self.rollback_ok:
            self.rolled_back.append(sale["payment_hash"])
        return self.rollback_ok


def fresh():
    node, game = FakeNode(), FakeGame()
    app.ESCROW_NODE = node
    app.RESOLVER = FakeResolver()
    app.game_execute = game.execute
    app.game_rollback = game.rollback
    return node, game


def new_sale(node, amount=100_000):
    body = app.SaleIn(item_id=1, buyer_id=2, seller_id=1, amount_msat=amount,
                      seller_payout={"type": "lnurl", "target": "a@b.c"})
    r = asyncio.run(app.create_sale(body, "test-secret"))
    import json
    sale = json.loads(r.body)
    return sale["sale_id"], sale["payment_hash"]


def run(sid, max_steps=30):
    """drive until terminal or stuck (state unchanged for 8 steps)"""
    same = 0
    last = None
    for _ in range(max_steps):
        s = app.load(sid)
        if s["state"] not in app.NON_TERMINAL:
            return s["state"]
        asyncio.run(app.drive(s))
        now = app.load(sid)["state"]
        same = same + 1 if now == last else 0
        last = now
        if same >= 8:
            return now
    return app.load(sid)["state"]


app.init_db()

print("== fee: base + ppm ==")
node, game = fresh()
sid, h = new_sale(node, 100_000)
s = app.load(sid)
check("fee = 1000 + 1% of 100000", 2000, s["fee_msat"])
check("seller gets the rest", 98_000, s["seller_amount_msat"])

print("== happy path ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
check("completes", "COMPLETED", run(sid))
check("buyer HTLC settled", "settled", node.hold[h])
check("routing fee capped at the sale fee", 2000, node.pay_calls[0][1])
check("payout CLTV capped below buyer HTLC", 200 - app.HOLD_SAFETY_BLOCKS,
      node.pay_calls[0][2])
check("game webhook queued", "completed", app.load(sid)["notify_result"])

print("== bug 1: payout still in flight is never rolled back ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
node.pay_result = "pending"
check("waits in PAYING_SELLER", "PAYING_SELLER", run(sid))
check("paid once, not retried while pending", 1, len(node.pay_calls))
check("no rollback", [], game.rolled_back)
check("no refund", [], node.cancelled)
node.payments["pay-seller-" + sid] = "complete"     # the stuck HTLC lands
check("then completes", "COMPLETED", run(sid))

print("== payout definitively failed x3 -> rollback, then refund ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
node.pay_result = "failed"
check("cancelled", "CANCELLED", run(sid))
check("3 attempts", 3, len(node.pay_calls))
check("item rolled back", [h], game.rolled_back)
check("buyer refunded", [h], node.cancelled)

print("== bug 2: buyer HTLC gone during a game outage -> no payout ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
game.execute_outcome = "down"
check("stuck executing", "EXECUTING", run(sid))
node.hold[h] = "cancelled"                           # LND auto-cancelled it
game.execute_outcome = "done"
check("rolled back + closed", "CANCELLED", run(sid))
check("seller never paid", [], node.pay_calls)
check("rollback issued", [h], game.rolled_back)

print("== bug 2: too few blocks left -> refund instead of paying ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
node.blocks_left = app.HOLD_SAFETY_BLOCKS + app.MIN_PAYOUT_CLTV - 1
check("cancelled", "CANCELLED", run(sid))
check("seller never paid", [], node.pay_calls)

print("== bug 2b: settle error is checked against the node ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
node.settle_error = "invoice already canceled"
orig_pay = node.pay_invoice


def pay_then_lose(*a):
    r = orig_pay(*a)
    node.hold[h] = "cancelled"                       # HTLC lost after payout
    return r


node.pay_invoice = pay_then_lose
check("ERROR, not COMPLETED", "ERROR", run(sid))
check("flagged as loss", True, app.load(sid)["failure_reason"].startswith("LOSS"))

node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"


def settle_lands_but_errors(preimage):
    node.hold[h] = "settled"                         # e.g. a retry after a timeout
    raise NodeError("invoice already settled")


node.settle_hold_invoice = settle_lands_but_errors
check("already settled -> COMPLETED", "COMPLETED", run(sid))

print("== bug 3: crash after paying -> recovered, not paid twice ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
app.set_state(sid, "PAYING_SELLER")
with app.tx() as c:
    c.execute("UPDATE sales SET seller_bolt11=?, seller_pay_hash=?, payout_attempts=1 "
              "WHERE sale_id=?", ("seller-" + sid, "pay-seller-" + sid, sid))
node.payments["pay-seller-" + sid] = "complete"
check("completes", "COMPLETED", run(sid))
check("no second payment", [], node.pay_calls)

print("== node unreachable -> decide nothing ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
app.set_state(sid, "PAYING_SELLER")
with app.tx() as c:
    c.execute("UPDATE sales SET seller_bolt11='x', seller_pay_hash='pay-x', "
              "payout_attempts=3 WHERE sale_id=?", (sid,))
node.unreachable = True
try:
    asyncio.run(app.drive(app.load(sid)))
except NodeError:
    pass
check("stays PAYING_SELLER", "PAYING_SELLER", app.load(sid)["state"])
check("no rollback", [], game.rolled_back)

print("== rollback unconfirmed -> no refund yet ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
app.set_state(sid, "ROLLING_BACK", "test")
game.rollback_ok = False
check("stays ROLLING_BACK", "ROLLING_BACK", run(sid))
check("buyer not refunded before item is back", [], node.cancelled)
game.rollback_ok = True
check("then cancelled", "CANCELLED", run(sid))

print("== rejected execute -> refund without rollback ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
game.execute_outcome = "rejected"
check("cancelled", "CANCELLED", run(sid))
check("refunded", [h], node.cancelled)

print("== create_sale guards ==")
from fastapi import HTTPException  # noqa: E402
node, game = fresh()
for label, payout in (("lab stub refused outside LAB_MODE", {"type": "stub"}),
                      ("missing target refused", {"type": "lnurl"})):
    try:
        asyncio.run(app.create_sale(app.SaleIn(item_id=1, buyer_id=2, seller_id=1,
                    amount_msat=100_000, seller_payout=payout), "test-secret"))
        check(label, 422, "accepted")
    except HTTPException as e:
        check(label, 422, e.status_code)
try:
    asyncio.run(app.create_sale(app.SaleIn(item_id=1, buyer_id=2, seller_id=1,
                amount_msat=100_000, seller_payout={"type": "lnurl", "target": "a@b.c"}),
                "wrong"))
    check("bad secret refused", 401, "accepted")
except HTTPException as e:
    check("bad secret refused", 401, e.status_code)

print("== payout invoice checks ==")
import payout  # noqa: E402


class DecodeNode:
    def __init__(self, amt):
        self.amt = amt

    def decode_invoice(self, b):
        return {"payment_hash": "decoded-hash", "amount_msat": self.amt}


try:
    payout.verify_invoice(DecodeNode(None), "x", 1000)
    check("amountless invoice refused", "PayoutError", "accepted")
except payout.PayoutError:
    check("amountless invoice refused", "PayoutError", "PayoutError")
check("hash comes from decoding", "decoded-hash",
      payout.verify_invoice(DecodeNode(1000), "x", 1000))

print(f"\nstate machine: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
