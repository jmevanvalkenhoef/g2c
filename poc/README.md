# g2c PoC — regtest Lightning escrow lab

Local, free-money replica of the production topology from
`../docs/ESCROW_SERVICE_SPEC.md`:

```
cln-buyer ──(channel)──▶ cln-escrow (+ BoltzExchange/hold) ──(channel)──▶ cln-seller
                              │
                              ▼
                        mock-game (game API with fault injection, host port 8080)
                              ▲
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
FAULT_MODE=reject docker compose up -d mock-game      # execute_sale → 409
FAULT_MODE=timeout docker compose up -d mock-game     # ambiguous outcomes → retry/sale_status
FAULT_MODE=server_error docker compose up -d mock-game
```

or per request: `curl -H 'X-Fault: timeout' ...`.

Crash drills: `docker compose kill cln-escrow && docker compose up -d cln-escrow`
while a payment is held — verifies timeout/reconciliation behavior.

## Notes / gotchas

- `elementsproject/lightningd:latest` — pin to the CLN version you validate with.
- The hold plugin binary is built from `BoltzExchange/hold` `main`; pin to a release
  tag for anything beyond the PoC.
- Channels are 500k sats; sale amounts ≤ 100k sats in drills.
- The escrow service skeleton (Phase 2) plugs in via `lightning-cli`/gRPC on
  cln-escrow and talks to `mock-game:8080` inside the compose network.
- `--dev-allow-localhost` and `--funding-confirms=1` are PoC conveniences; never
  copy this config to mainnet.
