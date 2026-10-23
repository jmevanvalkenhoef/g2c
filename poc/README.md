# g2c PoC — regtest Lightning escrow lab


> **Runbook**: see `../docs/RUNBOOK.md` for startup, the three watch panes,
> click-to-trace mapping, failure drills, and ledger inspection.
Local, free-money replica of the production topology from
`../docs/ESCROW_SERVICE_SPEC.md`, now including the **real escrow service**
(`../escrow/`) as a compose service:

```
cln-buyer ──(channel)──▶ cln-escrow (+ BoltzExchange/hold) ──(channel)──▶ cln-seller
                              ▲ RPC sockets (shared volumes)
                              │
                          escrow service (:8002 on host)
                              │ GAME_URL (host.docker.internal)
                     game-backend (host, :8000)
                              ▲
                        PWA / game-web
bitcoind (regtest) + miner (1 block / 15 s)
```

All coins are regtest sats — worthless, unlimited, mineable on demand.

## Run

```bash
cd poc/
docker compose build          # cln-escrow image compiles the hold plugin (~5-10 min once)
docker compose up -d
./scripts/setup.sh            # fund nodes, open the two channels
./scripts/hold-demo.sh settle # happy path: buyer pays, hold, settle
./scripts/hold-demo.sh cancel # failure path: buyer pays, hold, cancel → auto-refund
```

Inspect:

```bash
docker compose exec cln-escrow lightning-cli listholdinvoices
docker compose exec cln-buyer lightning-cli listpays
curl localhost:8080/debug/sales        # mock game DB state
```

## Fault injection (drives spec §6 failure matrix)

Restart with a global fault mode:

```bash
FAULT_MODE=reject docker compose up -d mock-game      # reserve_sale → 409
FAULT_MODE=timeout docker compose up -d mock-game     # ambiguous outcomes → retry/sale_status
FAULT_MODE=server_error docker compose up -d mock-game
```

or per request: `curl -H 'X-Fault: timeout' ...`.

Crash drills: `docker compose kill cln-escrow && docker compose up -d cln-escrow`
while a payment is held — verifies timeout/reconciliation behavior.

## Run the full stack (real Lightning)

```bash
docker compose build
./scripts/setup.sh                 # fund nodes, open channels (once per lab life)
# game backend on the HOST, reachable from docker (note --host 0.0.0.0):
cd ../game/game-backend && ESCROW_URL=http://localhost:8002 \
  uv run uvicorn app:app --host 0.0.0.0 --port 8000
# open http://localhost:8000 — buy flow now moves REAL regtest sats:
#   QR = genuine BOLT11 hold invoice; tap = cln-buyer pays it;
#   escrow holds the HTLC, transfers the item, pays cln-seller, settles.
```

Swap back to the stub anytime with `ESCROW_URL=http://localhost:8001`.

## Manual drills

```bash
./scripts/hold-demo.sh settle|cancel [sats]   # raw hold-invoice primitives
curl localhost:8002/v1/sales/<id>/fail -X POST -H 'X-Escrow-Secret: dev-secret'
docker compose restart escrow                 # crash-recovery drill:
                                              # held sales resume or fail safe
```

## Notes / gotchas

- `elementsproject/lightningd:latest` — pin to the CLN version you validate with.
- The hold plugin binary is built from `BoltzExchange/hold` `main`; pin to a release
  tag for anything beyond the PoC.
- Channels are 500k sats; the lab has survived 20k–70k sat sales in testing.
- `escrow` reaches the host via `host.docker.internal` (host-gateway) — the game
  MUST bind 0.0.0.0, not 127.0.0.1.
- The old `mock-game` service (fault injection) predates the real game backend;
  kept for contract-level fault drills only.
