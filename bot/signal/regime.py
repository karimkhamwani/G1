"""Chop/trend classifier for the current market window.

chop_score = (price range over the window so far) / |net move from strike|.
High score  -> price is oscillating around the open (choppy: ladder adds allowed).
Low score   -> one-way move (trending: layer 1 freezes).
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MIN_NET_FRAC = 0.0003   # below this net move (fraction of strike): "no character yet"
CHOP_CAP = 99.0
MIN_BARS = 30                   # fewer window bars than this and the read is not credible


@dataclass
class Regime:
    chop_score: float
    trending: bool
    net_move_frac: float
    known: bool = True          # False when there was not enough data to classify

    @property
    def label(self) -> str:
        if not self.known:
            return "unknown"
        return "trending" if self.trending else "choppy"


def classify(prices: list[float], strike: float, chop_score_min: float,
             min_net_frac: float = DEFAULT_MIN_NET_FRAC,
             min_bars: int = MIN_BARS) -> Regime:
    # Fail CLOSED: too little data is not evidence of chop. Reporting `trending`
    # here freezes the ladder, which is the safe direction — the old code returned
    # `choppy`, so a starved feed silently UNLOCKED averaging down.
    if len(prices) < min_bars or strike <= 0:
        return Regime(CHOP_CAP, True, 0.0, known=False)
    hi, lo, last = max(prices), min(prices), prices[-1]
    net = abs(last - strike)
    net_frac = net / strike
    if net_frac < min_net_frac:
        return Regime(CHOP_CAP, False, net_frac)
    score = min(CHOP_CAP, (hi - lo) / net)
    return Regime(score, score < chop_score_min, net_frac)
