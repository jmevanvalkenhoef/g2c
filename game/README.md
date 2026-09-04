# g2c demo game — vertical slice (Phase 2)

Three modules, one contract. **Hot-swappable by design**: game and escrow only ever
meet through the specced API (`docs/ESCROW_SERVICE_SPEC.md` §3.1/§3.2), authenticated
with the shared secret. Replace either side; nothing else changes.

```
game-backend (FastAPI + SQLite, :8000)          escrow-stub (FastAPI, :8001)
 ├─ /v1/...           player-facing (PWA)       ├─ POST /v1/sales  → payment_request
 ├─ /execute_sale  ◀──┼──────────────────────────┤   state machine: HTLC_HELD →
 ├─ /sale_status      │  HMAC-ish shared         │   EXECUTING → PAYING_SELLER → SETTLING
 ├─ /rollback_sale    │  secret (X-Escrow-Secret)│   (or ROLLING_BACK → CANCELLED)
 ├─ /internal/sale_result ◀ webhook (optional)  └─ debug: /fail /pause /resume
 └─ serves game-web/ at /
```

The stub mimics the real escrow's exact rhythm (1 s hold → execute → pay → settle,
failure → rollback + cancel). The future Lightning escrow implements the same two
surfaces — swapping is a `ESCROW_URL` config change.

## Run (uv)

```bash
# terminal 1 — game
cd game/game-backend && uv run uvicorn app:app --port 8000

# terminal 2 — escrow stub
cd game/escrow-stub && uv run uvicorn app:app --port 8001

# open http://localhost:8000 in two browser windows (two player names)
```

Env (defaults in parens): `ESCROW_URL` (localhost:8001) on the game;
`GAME_URL` (localhost:8000), `ESCROW_SHARED_SECRET` (dev-secret), `FEE_MSAT` (100),
`SALE_FAIL_EVERY=n` (every n-th sale fails) on the stub.

## Demo script

1. Window A: enter as `alice`, Inventory → claim a shop item → set price → **Sell**
2. Window B: enter as `bob`, Market → **Buy** → QR + "waiting for payment…"
3. Stub auto-pays after ~3 s → B sees "✅ paid", item moves to bob's inventory
4. Failure drill: set `SALE_FAIL_EVERY=2`, buy again → item stays with seller,
   purchase shows "❌ refunded" — money never moved
5. Debug controls on the stub:
   `curl -X POST -H 'X-Escrow-Secret: dev-secret' localhost:8001/v1/sales/<id>/fail|pause|resume`

## Contract notes (the part that makes swapping safe)

- `execute_sale` is **atomic + idempotent** on `idempotency_key` (= payment_hash):
  one SQLite transaction moves item ownership, completes the listing, upserts the
  sale row. Replays return `already_done`. 409 = definitive rejection.
- `rollback_sale` reverts ownership and reopens the listing; idempotent.
- The escrow never learns player names; the game never sees sats.
- SQLite rows `sales` / `purchases` are the reconciliation surface
  (`GET /sale_status?key=`).

## Known PoC simplifications (fine by design, fix later)

- "Payment" is auto-completed by the stub after ~3 s (no real wallet involved yet).
- Secret is a static header, not HMAC-with-nonce (spec §10 for production).
- Purchases list is client-side localStorage; polling every 2 s.
- One pending purchase per browser at a time.
