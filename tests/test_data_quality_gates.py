"""Gates that stop the bot trading on data it cannot trust.

Each test pins a failure seen live on 2026-09-02.
"""
import math
import time

from bot.feeds.spot import MIN_VOL_SAMPLES, SpotState
from bot.signal.fair_value import fair_yes


def _feed(state, start, n, step=1, vol=8e-5, seed=1.0):
    """Push n bars `step` seconds apart with a deterministic wiggle."""
    price = 77_000.0
    for i in range(n):
        price *= math.exp(vol * math.sin(seed * i) * math.sqrt(step))
        state.on_trade(price, start + i * step)
    return price


def test_sigma_ignores_returns_that_span_a_gap():
    """57% bar coverage produced a sigma 7-19x too low, saturating fair value."""
    st = SpotState("BTC", 1800, [5.0, 30.0])
    now = time.time() - 400
    _feed(st, now, 120)                       # contiguous stretch
    good = st.sigma_per_sqrt_s()
    st._sigma_cache = (0.0, 0.0)
    st.on_trade(78_500.0, now + 400)          # one huge jump across a 280s hole
    st._sigma_cache = (0.0, 0.0)
    assert abs(st.sigma_per_sqrt_s() - good) < good * 0.1, "gap step must not enter sigma"


def test_sigma_unusable_with_too_few_samples():
    st = SpotState("BTC", 1800, [5.0, 30.0])
    _feed(st, time.time() - 100, MIN_VOL_SAMPLES - 10)
    assert st.sigma_per_sqrt_s() == 0.0
    assert not st.vol_ready()


def test_stalled_tape_reports_no_estimate_not_zero_vol():
    """A repeating price is a dead feed, not a calm market."""
    st = SpotState("BTC", 1800, [5.0, 30.0])
    now = time.time() - 200
    for i in range(150):
        st.on_trade(77_000.0, now + i)        # identical price every second
    assert st.sigma_per_sqrt_s() == 0.0


def test_tiny_sigma_would_have_saturated_fair_value():
    """The live failure: BTC $3 below strike read as 19.5% instead of ~48%."""
    spot, strike, t_rem = 77_683.59, 77_686.6, 283
    assert fair_yes(spot, strike, 2.71e-06, 0.0, t_rem) < 0.25     # what it did
    assert 0.40 < fair_yes(spot, strike, 6.22e-05, 0.0, t_rem) < 0.60  # realized vol


def test_skew_blocked_when_regime_unknown():
    """Layer 2 is directional and unhedged: no classified regime, no skew."""
    import time

    from bot.models import BookTop, Market, Side
    from bot.settings import Settings
    from bot.signal.engine import SignalEngine
    from bot.signal.regime import classify
    from bot.state import Hub, MarketRuntime
    from tests.test_engine_gates import (CollectingExecutor, FakeSpot, NullRecorder,
                                         PermissiveRisk)

    now = time.time()
    m = Market(condition_id="c1", slug="t", question="t?", asset="BTC", duration_s=300,
               start_ts=now - 60, end_ts=now + 240,
               token={Side.YES: "ty", Side.NO: "tn"}, strike=100_000.0)
    rt = MarketRuntime(market=m)
    rt.books[Side.YES] = BookTop(bid=0.44, ask=0.46, bid_size=100, ask_size=900)
    rt.books[Side.NO] = BookTop(bid=0.54, ask=0.56, bid_size=900, ask_size=100)
    rt.position.base_placed = True
    hub = Hub(); hub.markets[m.condition_id] = rt
    eng = SignalEngine(Settings(_env_file=None, dashboard_port=0), hub,
                       {"BTC": FakeSpot(100_000.0, [100_000])}, PermissiveRisk(),
                       CollectingExecutor(), NullRecorder())

    rt.regime = classify([100_000, 100_050], 100_000.0, 2.0)      # starved -> unknown
    assert not rt.regime.known
    assert eng._skew(rt, {Side.YES: 0.05, Side.NO: 0.95}, 240) == []
    assert rt.confluence_dir == 0
