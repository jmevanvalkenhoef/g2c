# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy<2.2"]
# ///
"""Turns the simulated worlds into answers. Each world is one draw from the
priors, so a spread over worlds is the uncertainty that follows from them.

    uv run sim/analyze.py [sim/results/worlds.jsonl]

    2. Money fronted: how much the escrow pays sellers before buyers are collected.
    3. Fee structures: profit against refunds, the same worlds under each fee.
    4. Operations: sales needing a person, refunds, time in flight, stuck sales,
       and the alert thresholds that follow.
    5. What drives the outcomes: rank correlation of each world parameter
       with each outcome, over the worlds at the production fee.

Writes sim/results/summary.json.
"""

import json
import sys
from pathlib import Path

import numpy as np

SIM = Path(__file__).resolve().parent
PRODUCTION = (1000, 10000)


def q(x, *ps):
    return {f"p{p}": float(np.percentile(x, p)) for p in ps}


def ranks(x):
    return np.argsort(np.argsort(x)).astype(float)


def spearman(a, b):
    ra, rb = ranks(np.asarray(a)), ranks(np.asarray(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else SIM / "results" / "worlds.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    by_fee: dict[tuple, list] = {}
    for r in rows:
        by_fee.setdefault((r["fee_msat"], r["fee_ppm"]), []).append(r)
    for runs in by_fee.values():
        runs.sort(key=lambda r: r["seed"])
    prod = by_fee[PRODUCTION]
    seeds = [r["seed"] for r in prod]
    out = {"worlds": len(prod), "fee_structures": [list(k) for k in by_fee], "days": 7}

    # 0. Did any world break a money rule?
    out["problems"] = sum(len(r["problems"]) for r in rows)
    out["ledger_broken"] = sum(not r["ledger_ok"] for r in rows)
    out["sales_total"] = sum(r["sales"] for r in rows)

    # 2. Money fronted
    fronted = np.array([r["fronted_max_msat"] / 1000 for r in prod])
    hourly_volume = np.array([r["volume_msat"] / 1000 / (7 * 24) for r in prod])
    out["fronted"] = {
        "max_sats": q(fronted, 50, 90, 95, 99),
        "max_in_hours_of_volume": q(fronted / np.maximum(hourly_volume, 1), 50, 90, 95, 99),
        "end_sats_max": max(r["fronted_end_msat"] / 1000 for r in prod),
    }

    # 3. Fee structures, paired by world
    fees = {}
    base_profit = np.array([r["profit_msat"] for r in prod])
    for fee, runs in by_fee.items():
        assert [r["seed"] for r in runs] == seeds, "fee structures must cover the same worlds"
        paid = np.array([r["paid"] for r in runs])
        refunded = np.array([r["refunded"] for r in runs])
        no_route = np.array([r["faults"].get("no_route_within_fee", 0) for r in runs])
        profit = np.array([r["profit_msat"] for r in runs])
        volume = np.array([r["volume_msat"] for r in runs])
        days = [d["profit"] for r in runs for d in r["days"]]
        fees[f"{fee[0]}:{fee[1]}"] = {
            "profit_share_of_volume": q(profit / volume, 5, 50, 95),
            "profit_vs_production": q(profit / base_profit, 5, 50, 95),
            "refunds_per_1000_paid": q(1000 * refunded / paid, 5, 50, 95),
            "no_route_per_1000_paid": q(1000 * no_route / paid, 5, 50, 95),
            "losing_days": sum(p < 0 for p in days),
            "days": len(days),
        }
    out["fees"] = fees

    # 4. Operations, at the production fee
    sales = np.array([r["sales"] for r in prod])
    paid = np.array([r["paid"] for r in prod])
    flight = np.concatenate([r["in_flight_s"] for r in prod])
    out["operations"] = {
        "attention_per_1000_sales": q(1000 * np.array([r["needs_attention"] for r in prod]) / sales, 50, 90, 99),
        "refunds_per_1000_paid": q(1000 * np.array([r["refunded"] for r in prod]) / paid, 50, 90, 99),
        "refused_per_1000": q(1000 * np.array([r["refused"] for r in prod]) / (sales + np.array([r["refused"] for r in prod])), 50, 90, 99),
        "in_flight_s": q(flight, 50, 90, 99, 99.9),
        "stuck_max_per_week": q(np.array([r["stuck_max"] for r in prod]), 50, 90, 95, 99),
        "stuck_minutes_per_week": q(np.array([r["stuck_minutes"] for r in prod]), 50, 90, 99),
    }

    # 5. What drives the outcomes
    outcomes = {
        "fronted_in_hours_of_volume": fronted / np.maximum(hourly_volume, 1),
        "refunds_per_1000_paid": 1000 * np.array([r["refunded"] for r in prod]) / paid,
        "stuck_minutes": np.array([r["stuck_minutes"] for r in prod], float),
        "profit_share_of_volume": np.array([r["profit_msat"] / r["volume_msat"] for r in prod]),
    }
    names = list(prod[0]["params"])
    drivers = {}
    for o, y in outcomes.items():
        corr = {n: spearman([r["params"][n] for r in prod], y) for n in names}
        drivers[o] = dict(sorted(corr.items(), key=lambda kv: -abs(kv[1]))[:5])
    out["drivers"] = drivers

    (SIM / "results" / "summary.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
