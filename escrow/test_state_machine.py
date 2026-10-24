#!/usr/bin/env python3
"""State-machine money-safety tests: a fake node + fake game drive app.drive()
through the failure paths that used to pay the seller AND refund the buyer,
and the reserve -> pay -> settle -> commit ordering.

Run: uv run python test_state_machine.py
"""
import asyncio
import json
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
ADMIN = "t" * 32
os.environ["ADMIN_TOKENS"] = "tester:" + ADMIN

import app                                   # noqa: E402
import ledger                                # noqa: E402
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
        self.routing_fee = 300

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

    def payment_fee_msat(self, h):
        return self.routing_fee if self.payments.get(h) == "complete" else None

    def get_info(self):
        return {}


class FakeResolver:
    def resolve(self, payout, sale):
        bolt11 = f"seller-{sale['sale_id']}"
        return bolt11, "pay-" + bolt11


class FakeGame:
    """reserve/commit/release contract; an outcome of None = game down"""
    def __init__(self):
        self.reserved, self.committed, self.released = [], [], []
        self.reserve_outcome = "reserved"
        self.commit_outcome = "committed"
        self.release_outcome = "released"

    async def reserve(self, sale):
        if self.reserve_outcome == "reserved":
            self.reserved.append(sale["payment_hash"])
        return self.reserve_outcome

    async def commit(self, sale):
        if self.commit_outcome == "committed":
            self.committed.append(sale["payment_hash"])
        return self.commit_outcome

    async def release(self, sale):
        if self.release_outcome == "released":
            self.released.append(sale["payment_hash"])
        return self.release_outcome


def fresh():
    node, game = FakeNode(), FakeGame()
    app.ESCROW_NODE = node
    app.RESOLVER = FakeResolver()
    app.game_reserve = game.reserve
    app.game_commit = game.commit
    app.game_release = game.release
    return node, game


def new_sale(node, amount=100_000, **extra):
    body = app.SaleIn(item_id=1, buyer_id=2, seller_id=1, amount_msat=amount,
                      seller_payout={"type": "lnurl", "target": "a@b.c"}, **extra)
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
check("item committed", [h], game.committed)
check("routing fee capped at the sale fee", 2000, node.pay_calls[0][1])
check("payout CLTV capped below buyer HTLC", 200 - app.HOLD_SAFETY_BLOCKS,
      node.pay_calls[0][2])
check("game webhook queued", "completed", app.load(sid)["notify_result"])

print("== bug 1: payout still in flight is never released ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
node.pay_result = "pending"
check("waits in PAYING_SELLER", "PAYING_SELLER", run(sid))
check("paid once, not retried while pending", 1, len(node.pay_calls))
check("no release", [], game.released)
check("no refund", [], node.cancelled)
node.payments["pay-seller-" + sid] = "complete"     # the stuck HTLC lands
check("then completes", "COMPLETED", run(sid))

print("== payout definitively failed x3 -> release, then refund ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
node.pay_result = "failed"
check("cancelled", "CANCELLED", run(sid))
check("3 attempts", 3, len(node.pay_calls))
check("item released", [h], game.released)
check("never committed", [], game.committed)
check("buyer refunded", [h], node.cancelled)

print("== bug 2: buyer HTLC gone during a game outage -> no payout ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
game.reserve_outcome = None
check("stuck reserving", "RESERVING", run(sid))
node.hold[h] = "cancelled"                           # LND auto-cancelled it
game.reserve_outcome = "reserved"
check("released + closed", "CANCELLED", run(sid))
check("seller never paid", [], node.pay_calls)
check("release issued", [h], game.released)

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
check("item left frozen for a human", [], game.committed + game.released)

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
check("no release", [], game.released)

print("== release unconfirmed -> no refund yet ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
app.set_state(sid, "RELEASING", "test")
game.release_outcome = None
check("stays RELEASING", "RELEASING", run(sid))
check("buyer not refunded before item is back", [], node.cancelled)
game.release_outcome = "released"
check("then cancelled", "CANCELLED", run(sid))

print("== rejected reserve -> refund, never paid ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
game.reserve_outcome = "rejected"
check("cancelled", "CANCELLED", run(sid))
check("refunded", [h], node.cancelled)
check("seller never paid", [], node.pay_calls)

print("== commit after settle: game outage only delays delivery ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
game.commit_outcome = None
check("waits in COMMITTING", "COMMITTING", run(sid))
check("money already final", "settled", node.hold[h])
check("no release, no refund", ([], []), (game.released, node.cancelled))
game.commit_outcome = "committed"
check("then completes", "COMPLETED", run(sid))
check("delivered", [h], game.committed)

print("== commit rejected after payment -> ERROR, never refunded ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
game.commit_outcome = "rejected"
check("ERROR", "ERROR", run(sid))
check("flagged undelivered", True, app.load(sid)["failure_reason"].startswith("PAID, UNDELIVERED"))
check("buyer payment kept (settled)", "settled", node.hold[h])

print("== release rejected (game reports committed) -> ERROR, no refund ==")
node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
app.set_state(sid, "RELEASING", "test")
game.release_outcome = "rejected"
check("ERROR", "ERROR", run(sid))
check("not refunded", [], node.cancelled)

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

print("== audit trail + ledger ==")
import shutil  # noqa: E402
import sqlite3  # noqa: E402


def lines_of(sid):
    with app.db() as c:
        return {r["account"]: r["s"] for r in c.execute(
            "SELECT account, SUM(amount_msat) s FROM ledger WHERE sale_id=? "
            "GROUP BY account", (sid,))}


def path_of(sid):
    with app.db() as c:
        return [r["to_state"] for r in c.execute(
            "SELECT to_state FROM sale_events WHERE sale_id=? ORDER BY id", (sid,))]


node, game = fresh()
sid, h = new_sale(node, fiat_currency="EUR", fiat_amount="5.00")
node.hold[h] = "accepted"
check("completes", "COMPLETED", run(sid))
check("every step in the trail", ["AWAITING_PAYMENT", "HTLC_HELD", "RESERVING",
      "PAYING_SELLER", "SETTLING", "COMMITTING", "COMPLETED"], path_of(sid))
check("books: fee in, routing out, nothing left in clearing",
      {"assets:lightning": 2000 - 300, "expense:routing": 300,
       "revenue:fees": -2000}, {k: v for k, v in lines_of(sid).items() if v})
check("actual routing fee stored", 300, app.load(sid)["routing_fee_msat"])
check("payout target forgotten when done", None, app.load(sid)["seller_payout_tgt"])
with app.db() as c:
    ev = c.execute("SELECT data FROM sale_events WHERE sale_id=? AND "
                   "to_state='COMMITTING'", (sid,)).fetchone()
check("preimage recorded once settled (proof of payment)",
      app.load(sid)["preimage"], json.loads(ev["data"])["preimage"])
with app.db() as c:
    created = c.execute("SELECT data FROM sale_events WHERE sale_id=? AND "
                        "from_state IS NULL", (sid,)).fetchone()
check("no payout target in the trail", False, "a@b.c" in created["data"])
check("fiat price in the trail", "5.00", json.loads(created["data"])["fiat_amount"])

for sql in ("UPDATE sale_events SET reason='x'", "DELETE FROM ledger"):
    try:
        with app.tx() as c:
            c.execute(sql)
        check(f"refused: {sql}", "refused", "allowed")
    except sqlite3.DatabaseError:
        check(f"refused: {sql}", "refused", "refused")

node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
game.reserve_outcome = "rejected"
check("cancelled sale", "CANCELLED", run(sid))
check("cancelled sale books nothing", {}, lines_of(sid))

node, game = fresh()
sid, h = new_sale(node)
node.hold[h] = "accepted"
node.settle_error = "invoice already canceled"
orig_pay2 = node.pay_invoice


def pay_then_lose2(*a):
    r = orig_pay2(*a)
    node.hold[h] = "cancelled"
    return r


node.pay_invoice = pay_then_lose2
check("loss sale", "ERROR", run(sid))
check("loss written off, clearing back to 0",
      {"assets:lightning": -98_300, "expense:routing": 300,
       "expense:losses": 98_000}, {k: v for k, v in lines_of(sid).items() if v})
check("ERROR keeps the payout target for a human", "a@b.c",
      app.load(sid)["seller_payout_tgt"])

loss_sid = sid
for label, token, want in (("resolve needs a valid token", "nope", 401),):
    try:
        app.admin_resolve(loss_sid, app.ResolveIn(note="x"), token)
        check(label, want, 200)
    except HTTPException as e:
        check(label, want, e.status_code)
r = app.admin_resolve(loss_sid, app.ResolveIn(note="loss booked, seller kept"), ADMIN)
check("operator resolves ERROR", ("RESOLVED", "tester"), (r["state"], r["by"]))
with app.db() as c:
    last = c.execute("SELECT actor FROM sale_events WHERE sale_id=? "
                     "ORDER BY id DESC LIMIT 1", (loss_sid,)).fetchone()
check("trail names the operator", "operator:tester", last["actor"])
try:
    app.admin_resolve(loss_sid, app.ResolveIn(note="again"), ADMIN)
    check("only ERROR can be resolved", 409, 200)
except HTTPException as e:
    check("only ERROR can be resolved", 409, e.status_code)

today = ledger.now()[:10]
rep = app.admin_summary(today, today, ADMIN)
t = rep["totals"]
check("summary: net = fees - routing - losses",
      t["fees_msat"] - t["routing_msat"] - t["losses_msat"], t["net_msat"])
check("summary: exposure = clearing balance",
      rep["balances_msat"].get("clearing:sales", 0), rep["open_exposure_msat"])
check("summary: losses counted", True, t["losses_msat"] >= 98_000)

csv_text = app.admin_ledger_csv(today, today, ADMIN).body.decode()
check("csv: fee line valued in EUR", True,
      any(",revenue:fees,-2000," in ln and ",EUR,-0.1000," in ln
          for ln in csv_text.splitlines()))

v = app.admin_verify(None, None, ADMIN)
check("verify: chains intact, entries balance", True, v["ok"])
v2 = app.admin_verify(v["heads"]["sale_events"], v["heads"]["ledger"], ADMIN)
check("verify: anchored heads still in the chain", True, v2["ok"])
v3 = app.admin_verify("f" * 64, None, ADMIN)
check("verify: unknown anchor fails", False, v3["ok"])

copy = os.path.join(tempfile.mkdtemp(), "tampered.db")
with app.db() as c:
    c.execute("PRAGMA wal_checkpoint(FULL)")
shutil.copy(app.DB_PATH, copy)
tc = sqlite3.connect(copy)
tc.row_factory = sqlite3.Row
tc.execute("DROP TRIGGER ledger_no_update")
tc.execute("UPDATE ledger SET amount_msat=amount_msat+1 WHERE id=1")
tc.commit()
check("verify: an edited row breaks the chain", False, ledger.verify(tc)["ok"])
tc.close()

try:
    new_sale(node, fiat_currency="EUR")
    check("fiat needs both fields", 422, 201)
except HTTPException as e:
    check("fiat needs both fields", 422, e.status_code)
try:
    new_sale(node, fiat_currency="euro", fiat_amount="5")
    check("fiat currency must be ISO", 422, 201)
except HTTPException as e:
    check("fiat currency must be ISO", 422, e.status_code)

print(f"\nstate machine: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
