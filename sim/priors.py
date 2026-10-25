"""Priors for the simulated world. One call to `draw` gives one world: every
rate, amount parameter and rhythm the world runs on. The hierarchy:

    priors here  ->  one world's parameters  ->  healthy/degraded over time  ->  each sale and call

None of these numbers is measured; each is a stated assumption with a range,
so that a conclusion only counts when it holds across the range. Amounts are
in sats; one sat is roughly 0.05 to 0.1 euro cent at the time of writing.
"""

import math
import random
from dataclasses import asdict, dataclass


@dataclass
class World:
    # traffic
    sales_per_hour: float          # average over a week
    daily_amplitude: float         # 0 = flat, 1 = nothing at night
    peak_hour: float               # busiest hour of the day, UTC
    weekend_factor: float          # Saturday and Sunday traffic relative to weekdays
    # amounts
    amount_median_sats: float
    amount_sigma: float            # lognormal spread
    tail_share: float              # share of rare, valuable items
    # buyers
    pay_prob: float
    pay_delay_median_s: float
    # Lightning: the seller payout
    payout_fail_base: float        # failure chance for a payout of the median amount
    payout_fail_slope: float       # change in log-odds per e-fold in amount: large payments fail more
    routing_base_msat: float
    routing_ppm: float
    payout_stuck: float
    payout_stuck_median_s: float
    payout_stuck_lands: float
    payout_lost_answer: float
    settle_error: float
    settle_error_applied: float
    cancel_error: float
    resolve_fail: float
    # the game
    reserve_rejected: float
    game_lost_answer: float
    commit_rejected: float
    # outages and bad periods
    node_outages_per_day: float
    node_outage_median_s: float
    game_outages_per_day: float
    game_outage_median_s: float
    long_outage_share: float       # share of outages that last 20 times longer
    healthy_mean_h: float          # mean length of a healthy period
    degraded_mean_min: float       # mean length of a degraded period
    degraded_factor: float         # fault rates multiply by this while degraded

    def as_dict(self) -> dict:
        return asdict(self)


def beta(rng: random.Random, mean: float, strength: float) -> float:
    """Beta with the given mean; `strength` = a + b, higher is more certain."""
    return rng.betavariate(mean * strength, (1 - mean) * strength)


def lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0, sigma))


def gamma(rng: random.Random, mean: float, shape: float) -> float:
    return rng.gammavariate(shape, mean / shape)


def draw(rng: random.Random) -> World:
    return World(
        # A small marketplace: around ten sales an hour, with an evening
        # peak (gamers) and busier weekends.
        sales_per_hour=gamma(rng, 10, 4),
        daily_amplitude=0.9 * rng.betavariate(4, 4),
        peak_hour=rng.gauss(20, 1),
        weekend_factor=lognormal(rng, 1.3, 0.2),
        # In-game items: a few euros typically, with a long tail.
        amount_median_sats=lognormal(rng, 5000, 0.4),
        amount_sigma=gamma(rng, 1.0, 20),
        tail_share=rng.betavariate(1, 50),
        # Most buyers who open a payment pay; some walk away.
        pay_prob=beta(rng, 0.88, 25),
        pay_delay_median_s=lognormal(rng, 25, 0.4),
        # Seller payouts: a few percent fail, more so for large amounts
        # (less liquidity on the route); fees of about 1 sat plus 0.05%.
        payout_fail_base=beta(rng, 0.05, 40),
        payout_fail_slope=abs(rng.gauss(0.5, 0.25)),
        routing_base_msat=lognormal(rng, 1000, 0.5),
        routing_ppm=lognormal(rng, 500, 0.5),
        payout_stuck=beta(rng, 0.02, 100),
        payout_stuck_median_s=lognormal(rng, 1200, 0.5),
        payout_stuck_lands=beta(rng, 0.7, 10),
        payout_lost_answer=beta(rng, 0.03, 100),
        settle_error=beta(rng, 0.02, 100),
        settle_error_applied=beta(rng, 0.5, 10),
        cancel_error=beta(rng, 0.02, 100),
        resolve_fail=beta(rng, 0.03, 100),
        # The game: occasionally an item is gone, an answer lost; rarely a
        # reservation lost, which needs a person.
        reserve_rejected=beta(rng, 0.02, 100),
        game_lost_answer=beta(rng, 0.02, 100),
        commit_rejected=beta(rng, 0.001, 1000),
        # Outages: the node every few days for minutes, the game about daily;
        # a few last much longer.
        node_outages_per_day=gamma(rng, 0.5, 2),
        node_outage_median_s=lognormal(rng, 180, 0.5),
        game_outages_per_day=gamma(rng, 1.0, 2),
        game_outage_median_s=lognormal(rng, 600, 0.5),
        long_outage_share=rng.betavariate(1, 30),
        # Bad periods: a day or two healthy, then half an hour or so in which
        # the network and the game fail several times as often.
        healthy_mean_h=gamma(rng, 36, 3),
        degraded_mean_min=gamma(rng, 30, 3),
        degraded_factor=lognormal(rng, 8, 0.5),
    )
