# G2C

An escrow for digital goods that charges the buyer and pays the seller as one
step: the goods and the money move together, or neither moves. Payments run
over the Lightning Network, where a hold invoice keeps the buyer's payment
locked until the trade is done. The first use is trading in-game items.

- [White paper](docs/WHITE_PAPER.pdf): the service, the trade flow and the
  setup with a game studio (four pages).
- [Write-up](sim/writeup/): what it takes to run it, from the escrow's own
  state machine in 50 simulated weeks.

## How it fits together

```
escrow/   the escrow service: FastAPI, SQLite, a state machine that advances
          each sale one saved step at a time, a hash-chained event log and a
          double-entry ledger. Talks to Core Lightning (regtest) or LND (mainnet).
game/     a demo game: backend, phone web app, and an escrow stub for UI work
poc/      a regtest Lightning network in Docker: buyer, escrow and seller nodes,
          a mock game with fault modes, and crash drills
deploy/   signet and mainnet deployment: compose files, systemd units, runbooks
sim/      the simulation: priors, simulated node and game, runner, analysis,
          results and the write-up
docs/     white paper, service specification and runbook
```

## Run it

The escrow's money-safety tests, against a fake node and game:

```sh
cd escrow && uv run python test_state_machine.py
```

The full flow on a local Lightning network (see [poc/README.md](poc/README.md)):

```sh
cd poc
docker compose build && docker compose up -d
./scripts/setup.sh              # fund the nodes, open the channels
./scripts/hold-demo.sh settle   # buyer pays, the payment is held, then claimed
./scripts/hold-demo.sh cancel   # buyer pays, the payment is held, then returned
```

The simulation (see the write-up for what it measures):

```sh
cd escrow
uv run python ../sim/mutants.py     # the checks catch two deliberately broken escrows
uv run python ../sim/run.py --worlds 50 --days 7 \
    --fees 1000:10000,1000:5000,0:15000,5000:5000 --jobs 4
cd .. && uv run sim/analyze.py      # sim/results/summary.json
```

## Status

An earlier version of the contract has completed one real sale on the Bitcoin
mainnet. The current version runs end to end on regtest with fault and crash
drills, and in simulation.

## How it was built

With Claude Code as a pair programmer. The questions, the design decisions and
the review of every change are mine; commits it helped with carry a
`Co-Authored-By` line.

Design notes from before the build are in [docs/notes/](docs/notes/).
