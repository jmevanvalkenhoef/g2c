# G2C demo game — vertical slice (Phase 2)

Three modules, one contract. **Hot-swappable by design**: game and escrow only ever
meet through the specced API (`docs/ESCROW_SERVICE_SPEC.md` §3.1/§3.2), authenticated
with the shared secret. Replace either side; nothing else changes.

```
game-backend (FastAPI + SQLite, :8000)          escrow-stub (FastAPI, :8001)
 ├─ /v1/...           player-facing (PWA)       ├─ POST /v1/sales  → payment_request
 ├─ /reserve_sale  ◀──┼──────────────────────────┤   state machine: HTLC_HELD →
 ├─ /commit_sale      │  shared secret           │   RESERVING → PAYING_SELLER → SETTLING
 ├─ /release_sale     │  (X-Escrow-Secret)       │   → COMMITTING (or RELEASING → CANCELLED)
 ├─ /sale_status      │                          │
 ├─ /internal/sale_result ◀ webhook (optional)  └─ debug: /fail /pause /resume
 └─ serves game-web/ at /
```

The stub mimics the real escrow's exact rhythm (1 s hold → reserve → pay → settle
→ commit, failure → release + cancel). The future Lightning escrow implements the same two
surfaces — swapping is a `ESCROW_URL` config change.

## Run (uv)

```bash
# terminal 1 — game
cd game/game-backend && uv run uvicorn app:app --port 8000

# terminal 2 — escrow stub
cd game/escrow-stub && uv run uvicorn app:app --port 8001

# open http://localhost:8000 in two browser windows (two player names)
```

`POST /v1/players` creates a character (name, password, optional `ln_address`
and `role`) or signs back in to an existing one; the first login to a seeded
name (alice, bob) sets its password. Sessions are random tokens
(`X-Player-Session` header per tab, `session` cookie otherwise). Players edit
their own payout method behind that session; `GAME_ADMIN_TOKEN` guards only
the world reset.

Env (defaults in parens) on the game: `ESCROW_URL` (localhost:8001),
`ESCROW_SHARED_SECRET` (dev-secret). On the stub: `GAME_URL`
(localhost:8000), `ESCROW_SHARED_SECRET`, `SALE_FAIL_EVERY=n` (every n-th sale
fails). The fee is the **escrow's** revenue: `FEE_MSAT` (100) on the stub, returned
in the 201 as `fee_msat` + `seller_amount_msat`.

## Regression drill

`game/scripts/e2e.sh` boots both services and runs the full contract through
happy path (reserve → commit), failure path (reserve → release + relisting),
idempotency replays, and the double-pay guard — 15 checks, exits non-zero on failure. Run it after any change
to either side of the contract.

## Demo script

The web app is **The Trading Post**, a concept skin for a fantasy game where
players trade items (swords, potions, relics). Each player uses their own window, ideally their own computer:
their character on the left, the open market on the right, and a quest scroll
that narrates their side of the trade. "For game studios" (top right) opens the
partnership pitch.

1. Both people open the game (http://localhost:8000, or the server's address).
2. Each creates a character: any name, a password, a class, and the Lightning
   address their sales are paid to. Every new character gets five random
   starter items. Signing in later with the same name and password returns to it.
3. Person A clicks an item in their bag → names a price → **Put on offer**.
4. Person B sees it appear in the market → **Buy** → the trade window shows the
   invoice QR. Tap it (stub/lab) or scan it with a real wallet.
5. B watches the escrow steps light up (payment locked → item sealed → seller
   paid → payment claimed → item delivered). A sees "A trader is buying your …",
   the item sealed in their bag, then "Sold".
6. Failure drill: `SALE_FAIL_EVERY=2` on the stub, buy again → the item unseals
   and stays with the seller, the payment returns to the buyer.
7. Debug controls on the stub:
   `curl -X POST -H 'X-Escrow-Secret: dev-secret' localhost:8001/v1/sales/<id>/fail|pause|resume`

The seeded players `alice` and `bob` still exist for the automated drills.

## Contract notes (the part that makes swapping safe)

- `reserve_sale` **freezes** the item at the seller (`items.reserved_by`); a frozen
  item can't be listed, unlisted or sold again. Replays return `already_reserved`,
  409 = definitive rejection (item unavailable, or the key was already released).
- `commit_sale` hands the frozen item to the buyer. The escrow calls it only after
  the buyer's payment is final, and retries it until it lands.
- `release_sale` unfreezes the item and reopens its listing; refused once
  committed. Releasing an unknown key records it, so a late reserve can't land.
- Every call is one SQLite transaction, idempotent on `idempotency_key` (= payment_hash).
  Nothing is ever undone: ownership changes once, at commit.
- The escrow never learns player names; the game never sees sats.
- SQLite rows `sales` / `purchases` are the reconciliation surface
  (`GET /sale_status?key=`).

## Known PoC simplifications (fine by design, fix later)

- "Payment" is auto-completed by the stub after ~3 s (no real wallet involved yet).
- Secret is a static header, not HMAC-with-nonce (spec §10 for production).
- Purchases list is client-side localStorage; polling every 2 s.
- One pending purchase per browser at a time.
