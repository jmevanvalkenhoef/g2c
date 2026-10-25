"""Tests the checks in run.py: each mutant breaks one of the escrow's money
rules on purpose, and the run must report problems for it. A check that never
fires proves nothing.

    cd escrow && uv run python ../sim/mutants.py
"""

import asyncio
import itertools
import tempfile
from pathlib import Path

import run
import app


def ignores_payout_state(w):
    """Rule 1 removed: release and refund without asking whether the seller was paid."""
    app.payout_state = lambda sale: None


def forgets_payout_invoice(w):
    """The payout invoice is not saved: after a lost answer the seller gets a new invoice, and is paid again."""
    counter = itertools.count()
    app.resolve_payout = lambda sale: (f"seller-{sale['payment_hash']}#{next(counter)}",) * 2


MUTANTS = [ignores_payout_state, forgets_payout_invoice]


def main() -> None:
    run.QUERIES.update(run.load_queries())
    original = {name: getattr(app, name) for name in ("payout_state", "resolve_payout")}
    failed = 0
    for mutant in MUTANTS:
        found = []
        for seed in range(1, 6):
            db = Path(tempfile.mkdtemp()) / "escrow.db"
            result = asyncio.run(run.simulate(seed, 3, (1000, 10000), db, mutate=mutant))
            found += result["problems"]
            for name, fn in original.items():
                setattr(app, name, fn)
        caught = bool(found)
        failed += not caught
        kinds = sorted({p[1].split(" but ")[0] for p in found})
        print(f"{'caught' if caught else 'MISSED'}  {mutant.__name__}: {len(found)} problems in 5 worlds {kinds[:4]}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
