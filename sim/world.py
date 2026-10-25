"""The simulated world the escrow runs against: a clock, the chain, the
escrow's Lightning node, the game and the seller's payout address. It is also
the record of what really happened to each sale's money and item, so the run
can check the escrow's books against the truth.

Faults come from here, at rates drawn per world (priors.py). The world
switches between healthy and degraded periods; while degraded, every fault
rate is several times higher. The faults that test the escrow most are lost
answers: the node or the game does the work, but the escrow never hears back.
"""

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from node import NodeError
from payout import PayoutError

from priors import World as Params

START = datetime(2026, 11, 2, tzinfo=timezone.utc)   # a Monday
BLOCK_S = 600                                         # Bitcoin's average block interval


class Clock:
    def __init__(self) -> None:
        self.t = 0.0                    # seconds since START

    def now(self) -> datetime:
        return START + timedelta(seconds=self.t)


def clocked_datetime(clock: Clock):
    """A datetime class whose now() reads the simulation clock."""

    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now() if tz else clock.now().replace(tzinfo=None)

    return SimDatetime


def spans(rng: random.Random, per_day: float, median_s: float, long_share: float, until_s: float) -> list:
    """Outages: Poisson starts, lognormal lengths, a few 20 times longer."""
    out, t = [], 0.0
    while per_day > 0:
        t += rng.expovariate(per_day / 86_400)
        if t > until_s:
            return out
        length = median_s * math.exp(rng.gauss(0, 1.0)) * (20 if rng.random() < long_share else 1)
        out.append((t, t + length))
    return out


def markov(rng: random.Random, healthy_mean_s: float, degraded_mean_s: float, until_s: float) -> list:
    """Degraded periods of a two-state Markov chain that starts healthy."""
    out, t = [], 0.0
    while t < until_s:
        t += rng.expovariate(1 / healthy_mean_s)
        end = t + rng.expovariate(1 / degraded_mean_s)
        out.append((t, end))
        t = end
    return out


def inside(periods: list, t: float) -> bool:
    return any(a <= t < b for a, b in periods)


def logistic(x: float) -> float:
    return 1 / (1 + math.exp(-x))


@dataclass
class Payout:
    sale_hash: str                      # the buyer payment this payout belongs to
    state: str                          # pending | complete | failed
    resolves_at: float = 0.0
    final: str = "complete"
    fee_msat: int = 0


@dataclass
class Truth:
    """What actually happened to one sale's money and item."""
    amount_msat: int
    item: str = "free"                  # free | reserved | committed
    buyer: str = "unpaid"               # unpaid | held | settled | cancelled
    seller_payments: int = 0            # completed payouts for this sale
    notes: list = field(default_factory=list)


class World:
    def __init__(self, params: Params, seed: int, horizon_s: float) -> None:
        self.p = params
        self.rng = random.Random(seed)
        self.clock = Clock()
        r = self.rng
        self.node_down = spans(r, params.node_outages_per_day, params.node_outage_median_s, params.long_outage_share, horizon_s)
        self.game_down = spans(r, params.game_outages_per_day, params.game_outage_median_s, params.long_outage_share, horizon_s)
        self.degraded = markov(r, params.healthy_mean_h * 3600, params.degraded_mean_min * 60, horizon_s)
        self.faults_on = True
        self.height = 900_000
        self.next_block = r.expovariate(1 / BLOCK_S)
        self.invoices: dict[str, dict] = {}      # payment_hash -> {state, cltv, expiry_height, expires_at}
        self.payments: dict[str, Payout] = {}    # seller pay_hash -> payout
        self.truth: dict[str, Truth] = {}        # payment_hash -> truth
        self.item_gone: set[str] = set()         # items sold elsewhere before the reserve
        self.counts: dict[str, int] = {}         # faults that happened, by kind
        self.node = SimNode(self)
        self.game = SimGame(self)
        self.resolver = SimResolver(self)

    # -- time ------------------------------------------------------------

    def advance(self, dt: float) -> None:
        self.clock.t += dt
        while self.clock.t >= self.next_block:
            self.height += 1
            self.next_block += self.rng.expovariate(1 / BLOCK_S)
        for h, inv in self.invoices.items():
            if inv["state"] == "accepted" and self.height >= inv["expiry_height"]:
                inv["state"] = "cancelled"          # the node gives a held payment back at its timelock
                self.truth[h].buyer = "cancelled"
                self.truth[h].notes.append("held payment expired at the node")
            elif inv["state"] == "open" and self.clock.t > inv["expires_at"]:
                inv["state"] = "cancelled"
        for p in self.payments.values():
            if p.state == "pending" and self.clock.t >= p.resolves_at:
                p.state = p.final
                if p.state == "complete":
                    self.truth[p.sale_hash].seller_payments += 1

    # -- faults ----------------------------------------------------------

    def rate(self, p: float) -> float:
        if not self.faults_on:
            return 0.0
        return min(0.9, p * self.p.degraded_factor) if inside(self.degraded, self.clock.t) else p

    def happens(self, kind: str, p: float) -> bool:
        if self.rng.random() < self.rate(p):
            self.counts[kind] = self.counts.get(kind, 0) + 1
            return True
        return False

    def node_ok(self) -> bool:
        return not (self.faults_on and inside(self.node_down, self.clock.t))

    def game_ok(self) -> bool:
        return not (self.faults_on and inside(self.game_down, self.clock.t))

    # -- the buyer -------------------------------------------------------

    def buyer_pays(self, payment_hash: str) -> None:
        inv = self.invoices[payment_hash]
        if inv["state"] == "open":
            inv["state"] = "accepted"
            inv["expiry_height"] = self.height + inv["cltv"]
            self.truth[payment_hash].buyer = "held"


class SimNode:
    """The escrow's Lightning node: the NodeClient interface in node.py."""

    def __init__(self, w: World) -> None:
        self.w = w

    def _up(self) -> None:
        if not self.w.node_ok():
            raise NodeError("node unreachable")

    def get_info(self) -> dict:
        return {}

    def create_hold_invoice(self, h, amount_msat, expiry_s, cltv=None):
        self._up()
        self.w.invoices[h] = {"state": "open", "cltv": cltv or 288, "expires_at": self.w.clock.t + expiry_s}
        self.w.truth[h] = Truth(amount_msat)
        return f"lnbcrt-hold-{h[:12]}"

    def hold_invoice_state(self, h):
        if not self.w.node_ok():
            return None
        inv = self.w.invoices.get(h)
        return inv["state"] if inv else None

    def hold_blocks_left(self, h):
        self._up()
        inv = self.w.invoices.get(h)
        return inv["expiry_height"] - self.w.height if inv and inv["state"] == "accepted" else None

    def settle_hold_invoice(self, preimage):
        self._up()
        h = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        inv = self.w.invoices[h]
        if self.w.happens("settle_error", self.w.p.settle_error):
            if self.w.rng.random() < self.w.p.settle_error_applied and inv["state"] == "accepted":
                inv["state"] = "settled"
                self.w.truth[h].buyer = "settled"
            raise NodeError("settle: connection reset")
        if inv["state"] != "accepted":
            raise NodeError(f"settle: invoice is {inv['state']}")
        inv["state"] = "settled"
        self.w.truth[h].buyer = "settled"

    def cancel_hold_invoice(self, h):
        self._up()
        if self.w.happens("cancel_error", self.w.p.cancel_error):
            raise NodeError("cancel: connection reset")
        inv = self.w.invoices[h]
        if inv["state"] in ("open", "accepted"):
            inv["state"] = "cancelled"
            if self.w.truth[h].buyer == "held":
                self.w.truth[h].buyer = "cancelled"

    def pay_invoice(self, bolt11, timeout_s, fee_limit_msat=None, cltv_limit=None):
        self._up()
        pay_hash = "pay-" + bolt11
        existing = self.w.payments.get(pay_hash)
        if existing and existing.state in ("pending", "complete"):
            return existing.state == "complete", existing.state      # a node never pays an invoice twice
        sale_hash = bolt11.removeprefix("seller-").split("#")[0]   # mutants.py adds "#n" for a fresh invoice
        p, w = self.w.p, self.w
        amount = w.truth[sale_hash].amount_msat
        fee = int((p.routing_base_msat + p.routing_ppm * amount / 1e6) * math.exp(w.rng.gauss(0, 0.3)))
        fail = logistic(math.log(p.payout_fail_base / (1 - p.payout_fail_base))
                        + p.payout_fail_slope * math.log(amount / (1000 * p.amount_median_sats)))
        if fee_limit_msat is not None and fee > fee_limit_msat:
            w.counts["no_route_within_fee"] = w.counts.get("no_route_within_fee", 0) + 1
            payout = Payout(sale_hash, "failed")
        elif w.happens("payout_failed", fail):
            payout = Payout(sale_hash, "failed")
        elif w.happens("payout_stuck", p.payout_stuck):
            lands = w.rng.random() < p.payout_stuck_lands
            wait = p.payout_stuck_median_s * math.exp(w.rng.gauss(0, 1.0))
            payout = Payout(sale_hash, "pending", w.clock.t + wait, "complete" if lands else "failed", fee)
        else:
            payout = Payout(sale_hash, "complete", fee_msat=fee)
        w.payments[pay_hash] = payout
        if payout.state == "complete":
            w.truth[sale_hash].seller_payments += 1
            if w.happens("payout_lost_answer", p.payout_lost_answer):
                return False, "timeout (the payment went through)"
        return payout.state == "complete", payout.state

    def payment_state(self, pay_hash):
        self._up()
        p = self.w.payments.get(pay_hash)
        return p.state if p else None

    def payment_fee_msat(self, pay_hash):
        self._up()
        p = self.w.payments.get(pay_hash)
        return p.fee_msat if p and p.state == "complete" else None


class SimGame:
    """The game's side of the contract: reserve, commit, release. None = no answer."""

    def __init__(self, w: World) -> None:
        self.w = w

    def _answer(self, outcome):
        return None if self.w.happens("game_lost_answer", self.w.p.game_lost_answer) else outcome

    async def reserve(self, sale):
        h = sale["payment_hash"]
        t = self.w.truth[h]
        if not self.w.game_ok():
            return None
        if t.item == "free" and h not in self.w.item_gone and self.w.happens("item_gone", self.w.p.reserve_rejected):
            self.w.item_gone.add(h)
        if h in self.w.item_gone:
            return "rejected"
        if t.item == "free":
            t.item = "reserved"
        return self._answer("reserved" if t.item == "reserved" else "rejected")

    async def commit(self, sale):
        t = self.w.truth[sale["payment_hash"]]
        if not self.w.game_ok():
            return None
        if t.item == "reserved" and self.w.happens("reservation_lost", self.w.p.commit_rejected):
            t.item = "free"
            t.notes.append("the game lost the reservation")
        if t.item == "reserved":
            t.item = "committed"
        return self._answer("committed" if t.item == "committed" else "rejected")

    async def release(self, sale):
        t = self.w.truth[sale["payment_hash"]]
        if not self.w.game_ok():
            return None
        if t.item == "reserved":
            t.item = "free"
        return self._answer("released" if t.item == "free" else "rejected")


class SimResolver:
    """Resolves the seller's Lightning address to an invoice; sometimes fails."""

    def __init__(self, w: World) -> None:
        self.w = w

    def resolve(self, payout, sale):
        if self.w.happens("resolve_failed", self.w.p.resolve_fail):
            raise PayoutError("lightning address did not answer")
        bolt11 = "seller-" + sale["payment_hash"]
        return bolt11, "pay-" + bolt11
