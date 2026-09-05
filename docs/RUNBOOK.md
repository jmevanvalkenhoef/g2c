# g2c Runbook — running and observing the demo

Everything you need to spin up and watch the system. No need to dig through
chat history. Last updated: 2026-09-05.

## 1. Start everything (fresh world)

```bash
cd poc
docker compose down -v && docker compose up -d && ./scripts/setup.sh
#   wait for "=== topology ready ===" — it means: coinbases mature, channels
#   open, AND gossip propagated (payments work immediately after this line)

# separate terminal — the game (host):
cd game/game-backend
ESCROW_URL=http://localhost:8002 uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

- PWA: `http://localhost:8000` (two windows = two players: Alice / Bob)
- Swap to fake-money escrow stub: `ESCROW_URL=http://localhost:8001` + run
  `game/escrow-stub` on 8001 (see `game/README.md`)
- Contract regression drill: `game/scripts/e2e.sh`
- Sanity: `curl -s localhost:8002/v1/health` → `{"ok":true,...}`
- Reset game world only: restart the game process (DB wipes, Alice/Bob re-seeded)
- Reset everything: the `down -v` ritual above

## 2. The three watch panes (open before clicking around)

```bash
# PANE 1 — the escrow's brain: every state transition prints here
cd poc && docker compose logs -f escrow
#   e.g. [escrow] dbff7d2ca84d -> HTLC_HELD

# PANE 2 — the game backend: HTTP requests + contract calls
#   = the terminal where uvicorn runs

# PANE 3 — the money: held invoices, buyer wallet, channel balances
cd poc && watch -n1 '
  echo "== held invoices at escrow node =="
  docker compose exec -T cln-escrow lightning-cli --regtest listholdinvoices | python3 -c "import json,sys; [print(\" \",i[\"payment_hash\"][:12],i[\"state\"]) for i in json.load(sys.stdin)[\"holdinvoices\"]]"
  echo "== buyer wallet =="
  docker compose exec -T cln-buyer lightning-cli --regtest listpays | python3 -c "import json,sys; [print(\" \",p[\"payment_hash\"][:12],p[\"status\"]) for p in json.load(sys.stdin)[\"pays\"]]"
  echo "== escrow channel balances =="
  docker compose exec -T cln-escrow lightning-cli --regtest listfunds | python3 -c "import json,sys; [print(\" \",c[\"peer_id\"][:8],c[\"our_amount_msat\"]) for c in json.load(sys.stdin)[\"channels\"]]"'
```

## 3. Click → what happens → where to watch

| UI action | Under the hood | Watch |
|---|---|---|
| **Sell** (set price) | `POST /v1/listings` → row in game DB | pane 2 |
| **Buy** | listing → `in_flight`; escrow generates preimage, node gets hash only; `holdinvoice` → real `lnbcrt…` QR | pane 3: new invoice `unpaid` |
| **Tap QR** | game → escrow `/pay` → cln-buyer pays the invoice → **real HTLC locks** | pane 3: invoice `accepted`, buyer payment `pending` — *that pending is the escrow holding your money* |
| (automatic, ~5–20s) | `HTLC_HELD → EXECUTING` (item transfer in one SQL tx) `→ PAYING_SELLER` (invoice at cln-seller, paid from escrow's channel) `→ SETTLING` (preimage released) `→ COMPLETED` + webhook | pane 1 prints each transition; pane 3: buyer `complete`, outbound channel drops by net amount |
| **✅ paid** | PWA poll flips the pill; item moves to buyer | both windows |

Typical total: tap → ✅ in 15–25s. Nearly all of it is CLN `pay` latency on the
two payment legs; the state machine itself transitions in <1s per state.

## 4. Failure drills

```bash
# AUTOMATED regression drills (Sprint A — run before any change you care about):
game/scripts/e2e.sh            # 12 checks vs the stub (boots both services)
game/scripts/e2e.sh --real     # 14 checks vs the REAL escrow (lab must be up)
python3 poc/scripts/kill_drill.py   # crash escrow at EVERY state transition;
                                    # 4/4 must land in legal terminal states
poc/scripts/fault-drill.sh     # game-side faults: reject -> refund,
                               # timeout -> hold & retry, recovery -> complete

# manual drills:

```bash
# force the next sale's payout to fail → rollback + auto-refund:
curl -X POST localhost:8002/v1/sales/<sale_id>/fail -H 'X-Escrow-Secret: dev-secret'

# crash the escrow mid-sale (tap QR, then immediately):
docker compose restart escrow     # reconcile loop resumes held sales on startup

# game crash mid-sale (tap QR, then): restart the game process
#   → execute_sale 409s (stateless DB wiped the listing) → cancel → refund
```

## 5. Peeking at the ledgers

```bash
# game DB (items, listings, sales, purchases):
cd game/game-backend && uv run python -c "
import sqlite3; c=sqlite3.connect('data/game.db'); c.row_factory=sqlite3.Row
for t in ('items','listings','sales','purchases'):
    print(t, [dict(r) for r in c.execute(f'select * from {t}')])"

# escrow DB (states, payout hashes; preimages live here too):
cd poc && docker compose exec -T escrow python -c "
import sqlite3; c=sqlite3.connect('/app/data/escrow.db')
for r in c.execute('select sale_id,state,failure_reason from sales'): print(r)"

# raw node inspection:
docker compose exec -T cln-escrow lightning-cli --regtest listholdinvoices
docker compose exec -T cln-buyer  lightning-cli --regtest listpays
docker compose exec -T cln-seller lightning-cli --regtest listinvoices
```

## 6. Capacity, restarts, and quirks

- **Channel capacity is consumed by volume, not concurrency**: the in/out
  channels drain one-way (buyer → escrow → seller). When payouts start
  failing, run `poc/scripts/topup.sh` (opens fresh channels, no data loss).
- **After a host/docker restart**: peers may not reconnect by themselves —
  rerun `./scripts/setup.sh` (idempotent; or topup.sh's connect lines).
  The miner now re-loads its wallet after bitcoind restarts.
- Fresh channels need 6+ confirmations + gossip before payments route
  (setup.sh and topup.sh both wait for this).

## 7. Signet / production

See `deploy/README.md` — VPS ritual, faucet funding, phone onboarding.

- Fresh channels need 6+ confirmations + gossip before payments route —
  `setup.sh` waits for this, so only relevant if you bypass it.
- `pay` to a *held* invoice blocks until settle/cancel — that's the escrow
  working, not a hang (which is why `/pay` is fire-and-forget).
- Sale latency varies 15–25s; the variance is CLN `pay`, not our logic.
