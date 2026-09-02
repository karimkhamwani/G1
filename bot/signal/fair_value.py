"""Fair-value model: probability the window closes above its open price.

fair_yes = Φ( (ln(spot/strike) + drift·t) / (σ·√t) )
sigma is the per-√second volatility of log returns; drift is log-return rate per second.
"""
from __future__ import annotations

import math

MIN_SIGMA = 1e-6  # floor so a dead-quiet tape doesn't divide by zero


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_yes(spot: float, strike: float, sigma_s: float, drift_s: float, t_rem_s: float) -> float:
    if spot <= 0 or strike <= 0:
        return 0.5
    if t_rem_s <= 0:
        # tie rule: Up wins only if the close is strictly ABOVE the open
        return 1.0 if spot > strike else 0.0
    sigma = max(sigma_s, MIN_SIGMA)
    num = math.log(spot / strike) + drift_s * t_rem_s
    den = sigma * math.sqrt(t_rem_s)
    return norm_cdf(num / den)


def effective_cost(price: float, fee_bps: float) -> float:
    """Price plus Polymarket's fee, which scales with min(p, 1-p)."""
    return price + (fee_bps / 10_000.0) * min(price, 1.0 - price)
