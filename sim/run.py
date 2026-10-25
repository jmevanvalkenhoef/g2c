"""Runs the real escrow state machine (escrow/app.py, unchanged) through
simulated worlds and reads the dashboard telemetry from its database.

    cd escrow && uv run python ../sim/run.py --worlds 100 --days 7 [--fees 1000:10000,0:15000] [--jobs 4]

For each world: draw its parameters (priors.py), generate a week of sales
with a daily rhythm, and step the escrow on a simulated clock while the
world (world.py) injects faults. Every simulated minute the run takes the
dashboard's snapshot (stuck sales, money fronted). After the week, faults
stop and every open sale gets six hours to finish. Then each sale is checked
against what really happened, and the telemetry queries run on the database.
One line per world and fee structure goes to sim/results/worlds.jsonl. With
several fee structures, every world runs under each of them with the same
random draws, so a difference between them is the fee's alone.
"""

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

SIM = Path(__file__).resolve().parent
ESCROW = SIM.parent / "escrow"
sys.path[:0] = [str(ESCROW), str(SIM)]
os.environ.update(DB_PATH=str(Path(tempfile.mkdtemp()) / "escrow.db"), LAB_MODE="0", NODE_BACKEND="cln",
                  ESCROW_SHARED_SECRET="sim", ADMIN_TOKENS="")

import app       # noqa: E402  the escrow, as deployed
import ledger    # noqa: E402

import priors    # noqa: E402
import world as sim_world  # noqa: E402

TICK_S = 5.0            # the escrow's loop runs every 0.5 s; 5 s of simulated time per step is enough here
SNAPSHOT_S = 60.0       # how often the "dashboard" looks
DRAIN_S = 6 * 3600      # after the week: no new sales, no faults, time to finish
STUCK_AFTER_S = 600     # the dashboard's definition of stuck
QUERIES = {}


def load_queries() -> dict:
    name, lines, out = None, [], {}
    for line in (SIM / "telemetry.sql").read_text().splitlines():
        if line.startswith("-- name: "):
            if name:
                out[name] = "\n".join(lines)
            name, lines = line.removeprefix("-- name: ").strip(), []
        elif not line.startswith("--"):
            lines.append(line)
    out[name] = "\n".join(lines)
    return out


async def direct(fn, *args, **kwargs):
    """asyncio.to_thread without the thread: the simulation is single-threaded and deterministic."""
    return fn(*args, **kwargs)


def arrivals(p: priors.World, rng: random.Random, horizon_s: float) -> list:
    """Sales as a Poisson process with a daily rhythm and busier weekends (thinning)."""
    def shape(t):
        hour = (t / 3600) % 24
        day = int(t // 86_400) % 7
        daily = 1 + p.daily_amplitude * math.cos(2 * math.pi * (hour - p.peak_hour) / 24)
        return daily * (p.weekend_factor if day >= 5 else 1.0)

    mean_shape = sum(shape(m * 60) for m in range(7 * 24 * 60)) / (7 * 24 * 60)
    peak = (1 + p.daily_amplitude) * max(1.0, p.weekend_factor)
    out, t = [], 0.0
    while True:
        t += rng.expovariate(p.sales_per_hour * peak / mean_shape / 3600)
        if t > horizon_s:
            return out
        if rng.random() < shape(t) / peak:
            out.append({"t": t, "amount_sats": amount(p, rng), "pays": rng.random() < p.pay_prob,
                        "delay": p.pay_delay_median_s * math.exp(rng.gauss(0, 0.8))})


def amount(p: priors.World, rng: random.Random) -> int:
    if rng.random() < p.tail_share:
        sats = 20 * p.amount_median_sats * rng.paretovariate(1.5)
    else:
        sats = p.amount_median_sats * math.exp(rng.gauss(0, p.amount_sigma))
    return int(min(max(sats, 100), 2_000_000))


def wire(w: sim_world.World, db_path: Path) -> None:
    """Point the escrow at the simulated world and a fresh database."""
    app.DB_PATH = db_path
    app.ESCROW_NODE = w.node
    app.RESOLVER = w.resolver
    app.game_reserve, app.game_commit, app.game_release = w.game.reserve, w.game.commit, w.game.release
    app.datetime = ledger.datetime = sim_world.clocked_datetime(w.clock)
    asyncio.to_thread = direct
    app.init_db()


async def simulate(seed: int, days: float, fee: tuple[int, int], db_path: Path, mutate=None) -> dict:
    rng = random.Random(seed)
    params = priors.draw(rng)
    horizon = days * 86_400
    w = sim_world.World(params, seed, horizon)
    app.FEE_MSAT, app.FEE_PPM = fee
    wire(w, db_path)
    if mutate:
        mutate(w)       # mutants.py: break the escrow on purpose, to test the checks

    queue = arrivals(params, rng, horizon)
    pending_pay: list[tuple[float, str]] = []
    refused, snapshots = 0, []
    next_snapshot = 0.0
    reader = sqlite3.connect(db_path)          # the run's own read connection; the escrow keeps writing through app.db()
    reader.row_factory = sqlite3.Row
    open_ids: list[str] = []                   # sales not yet in a terminal state, in creation order

    while True:
        t = w.clock.t
        if t >= horizon:
            w.faults_on = False
        while queue and queue[0]["t"] <= t:
            s = queue.pop(0)
            body = app.SaleIn(item_id=1, buyer_id=2, seller_id=3, amount_msat=s["amount_sats"] * 1000,
                              seller_payout={"type": "lnurl", "target": "seller@example.com"})
            try:
                r = json.loads((await app.create_sale(body, "sim")).body)
            except app.HTTPException:
                refused += 1        # node down: the game shows an error, the buyer tries later or not
                continue
            open_ids.append(r["sale_id"])
            if s["pays"]:
                pending_pay.append((t + s["delay"], r["payment_hash"]))
        for due, h in [x for x in pending_pay if x[0] <= t]:
            w.buyer_pays(h)
            pending_pay.remove((due, h))

        active = []
        for sid in list(open_ids):
            sale = dict(reader.execute("SELECT * FROM sales WHERE sale_id = ?", (sid,)).fetchone())
            if sale["state"] not in app.NON_TERMINAL:
                open_ids.remove(sid)
                continue
            active.append(sid)
            if not idle(w, sale, t):
                await app._drive_one(sid)      # the reconcile loop's own step: fresh row, errors caught, retried next tick

        if t >= next_snapshot and t < horizon:
            snapshots.append(snapshot(w, t, reader))
            next_snapshot += SNAPSHOT_S
        if t >= horizon + DRAIN_S or (t >= horizon and not active and not pending_pay):
            break
        if not active and not pending_pay and queue and t < horizon:
            w.advance(max(TICK_S, min(queue[0]["t"], next_snapshot) - t))   # nothing to do until then
        else:
            w.advance(TICK_S)

    reader.close()
    return report(seed, params, fee, w, db_path, refused, snapshots, horizon)


def idle(w: sim_world.World, sale: dict, t: float) -> bool:
    """A sale awaiting payment whose invoice is still open and unexpired: driving it
    would only ask the node and change nothing, so skip it (saves most of the run time)."""
    if sale["state"] != "AWAITING_PAYMENT" or not w.node_ok():
        return False
    inv = w.invoices[sale["payment_hash"]]
    return inv["state"] == "open" and t <= inv["expires_at"] + 60


def snapshot(w: sim_world.World, t: float, conn: sqlite3.Connection) -> dict:
    now = w.clock.now().isoformat()
    stuck = len(conn.execute(QUERIES["stuck_now"], {"now": now}).fetchall())
    fronted = conn.execute(QUERIES["fronted_now"]).fetchone()[0]
    return {"t": t, "stuck": stuck, "fronted_msat": fronted, "degraded": sim_world.inside(w.degraded, t)}


def check(w: sim_world.World, conn: sqlite3.Connection) -> list:
    """Each sale's ending against what really happened to its money and item."""
    problems = []
    for row in conn.execute("SELECT sale_id, payment_hash, state, failure_reason FROM sales"):
        sid, h, state, reason = row
        t = w.truth[h]
        if t.seller_payments > 1:
            problems.append((sid, "seller paid twice"))
        if state == "COMPLETED" and not (t.buyer == "settled" and t.seller_payments == 1 and t.item == "committed"):
            problems.append((sid, f"COMPLETED but buyer={t.buyer} seller_paid={t.seller_payments} item={t.item}"))
        elif state == "CANCELLED" and not (t.buyer != "settled" and t.seller_payments == 0 and t.item == "free"):
            problems.append((sid, f"CANCELLED but buyer={t.buyer} seller_paid={t.seller_payments} item={t.item}"))
        elif state == "ERROR" and not reason.startswith("PAID, UNDELIVERED"):
            problems.append((sid, f"ERROR: {reason}"))
        elif state in app.NON_TERMINAL:
            problems.append((sid, f"still {state} after the drain"))
    return problems


def report(seed, params, fee, w, db_path, refused, snapshots, horizon) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        days = [dict(r) for r in conn.execute(QUERIES["money_per_day"])]
        outcomes = [dict(r) for r in conn.execute(QUERIES["outcomes"])]
        attention = len(conn.execute(QUERIES["needs_attention"]).fetchall())
        fronted_end = conn.execute(QUERIES["fronted_now"]).fetchone()[0]
        verified = ledger.verify(conn)["ok"]
        flight = in_flight_seconds(conn)
        problems = check(w, conn)
        sales = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    paid = sum(1 for t in w.truth.values() if t.buyer != "unpaid")
    refunded = sum(1 for t in w.truth.values() if t.buyer == "cancelled")   # paid, then given back
    week = [d for d in days if d["day"] < (sim_world.START + timedelta(seconds=horizon)).date().isoformat()]
    return {
        "seed": seed, "fee_msat": fee[0], "fee_ppm": fee[1], "params": params.as_dict(),
        "sales": sales, "refused": refused, "paid": paid, "refunded": refunded,
        "outcomes": outcomes, "needs_attention": attention,
        "days": week, "profit_msat": sum(d["profit"] for d in days), "volume_msat": sum(d["volume"] for d in days),
        "fronted_max_msat": max((s["fronted_msat"] for s in snapshots), default=0),
        "fronted_end_msat": fronted_end,
        "stuck_max": max((s["stuck"] for s in snapshots), default=0),
        "stuck_minutes": sum(s["stuck"] > 0 for s in snapshots),
        "in_flight_s": flight,
        "faults": w.counts,
        "degraded_share": sum(s["degraded"] for s in snapshots) / max(1, len(snapshots)),
        "ledger_ok": verified,
        "problems": problems,
    }


def in_flight_seconds(conn: sqlite3.Connection) -> list:
    """For every paid sale: seconds from the buyer's payment arriving to the sale's end."""
    rows = conn.execute("""
        SELECT a.at AS start, b.at AS end FROM sale_events a JOIN sale_events b ON a.sale_id = b.sale_id
        WHERE a.to_state = 'HTLC_HELD' AND b.to_state IN ('COMPLETED', 'CANCELLED', 'ERROR')""").fetchall()
    return [round((datetime.fromisoformat(r[1]) - datetime.fromisoformat(r[0])).total_seconds(), 1) for r in rows]


def one(seed: int, days: float, fee: tuple[int, int]) -> dict:
    """One world under one fee structure, in a worker process; the escrow's own log is silenced."""
    if not QUERIES:
        QUERIES.update(load_queries())
    started = time.time()
    folder = Path(tempfile.mkdtemp())
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet):
        result = asyncio.run(simulate(seed, days, fee, folder / "escrow.db"))
    shutil.rmtree(folder)
    result["wall_s"] = round(time.time() - started, 1)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worlds", type=int, default=1)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fees", default="1000:10000", help="fee structures as base_msat:ppm, comma-separated; production first")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--out", type=Path, default=SIM / "results" / "worlds.jsonl")
    args = ap.parse_args()
    fees = [tuple(int(x) for x in f.split(":")) for f in args.fees.split(",")]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    runs = [(args.seed + i, fee) for i in range(args.worlds) for fee in fees]
    with ProcessPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(one, seed, args.days, fee) for seed, fee in runs]
        for n, future in enumerate(as_completed(futures), 1):
            try:
                r = future.result()
            except Exception as e:           # one broken world must not cost the batch
                print(f"{n:4}/{len(runs)} a world failed: {e!r}", flush=True)
                continue
            with args.out.open("a") as f:
                f.write(json.dumps(r) + "\n")
            print(f"{n:4}/{len(runs)} world {r['seed']} fee {r['fee_msat']}:{r['fee_ppm']}: {r['sales']} sales, "
                  f"profit {r['profit_msat'] / 1000:.0f} sat, fronted max {r['fronted_max_msat'] / 1000:.0f} sat, "
                  f"stuck max {r['stuck_max']}, attention {r['needs_attention']}, problems {len(r['problems'])}, "
                  f"ledger {'ok' if r['ledger_ok'] else 'BROKEN'}, {r['wall_s']} s", flush=True)


if __name__ == "__main__":
    main()
