"""The gates that keep Layer 1 from averaging into a losing trend."""
import random
import time

from bot.models import Action, BookTop, Fill, Market, Side, SignalType
from bot.settings import Settings
from bot.signal.engine import SignalEngine
from bot.signal.regime import MIN_BARS, classify
from bot.state import Hub, MarketRuntime


class FakeSpot:
    def __init__(self, price, prices, sigma=1e-4, drift=0.0):
        self.price = price
        self.ts = time.time()
        # classify() needs MIN_BARS samples before it trusts a read; repeating the
        # series preserves its high, low and last value, so the regime is unchanged
        if prices:
            reps = -(-(MIN_BARS + 1) // len(prices))
            prices = list(prices) * max(reps, 1)
        self._prices = prices
        self._sigma = sigma
        self._drift = drift
        self.stale = 0.0

    def sigma_per_sqrt_s(self):
        return self._sigma

    def drift_per_s(self):
        return self._drift

    def prices_since(self, ts):
        return self._prices


class CollectingExecutor:
    def __init__(self):
        self.intents = []

    def submit(self, intent):
        self.intents.append(intent)


class PermissiveRisk:
    def validate(self, intent, rt):
        return None


class NullRecorder:
    def log(self, *a, **k):
        pass


def make_env(spot_prices, spot_now, book_yes, book_no, position_fills=(), drift=0.0):
    s = Settings(_env_file=None, dashboard_port=0, strategy="paired")
    hub = Hub()
    now = time.time()
    m = Market(condition_id="c1", slug="test", question="test?", asset="BTC",
               duration_s=300, start_ts=now - 120, end_ts=now + 180,
               token={Side.YES: "ty", Side.NO: "tn"}, strike=100_000.0)
    rt = MarketRuntime(market=m)
    rt.books[Side.YES] = book_yes
    rt.books[Side.NO] = book_no
    rt.position.base_placed = True
    for f in position_fills:
        rt.position.apply_fill(f)
    hub.markets["c1"] = rt
    ex = CollectingExecutor()
    eng = SignalEngine(s, hub, {"BTC": FakeSpot(spot_now, spot_prices, drift=drift)},
                       PermissiveRisk(), ex, NullRecorder(), rng=random.Random(7))
    return s, hub, rt, ex, eng


def top(bid, ask, bid_depth=500, ask_depth=500):
    return BookTop(bid=bid, bid_size=100, ask=ask, ask_size=100,
                   bid_depth_usdc=bid_depth, ask_depth_usdc=ask_depth, ts=time.time())


def buy(side, price, shares, signal=SignalType.BASE_ENTRY):
    return Fill(market_id="c1", side=side, action=Action.BUY, price=price,
                shares=shares, fee=0.0, signal=signal)


def test_no_pair_add_when_pair_is_not_cheap():
    # one-way move: NO got cheap but YES got expensive — the PAIR (0.88 + 0.12 = 1.00)
    # is not below PAIR_ADD_MAX, so no add: pairs only, never one-sided chasing
    trend_prices = [100_000 + i * 40 for i in range(11)]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    _, _, rt, ex, eng = make_env(trend_prices, 100_400, top(0.85, 0.88), top(0.10, 0.12), fills)
    eng.evaluate(rt)
    assert not [i for i in ex.intents if i.signal is SignalType.SCALE_ADD]


def test_normal_spread_pair_is_too_expensive():
    # a normal book (0.62 + 0.40 = 1.02) never triggers a pair add — buying both
    # sides above $1 combined is a guaranteed loss on matched shares
    chop = [100_000, 100_060, 99_950, 100_050, 99_960, 100_040]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    _, _, rt, ex, eng = make_env(chop, 100_050, top(0.60, 0.62), top(0.38, 0.40),
                                 fills, drift=2e-6)
    eng.evaluate(rt)
    assert not [i for i in ex.intents if i.signal is SignalType.SCALE_ADD]


def test_pair_add_fires_on_both_sides():
    # combined ask 0.64 + 0.33 = 0.97 <= PAIR_ADD_MAX: buy BOTH sides, equal shares
    chop = [100_000, 100_080, 99_920, 100_070, 99_930, 100_005]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    _, _, rt, ex, eng = make_env(chop, 100_000, top(0.60, 0.64), top(0.31, 0.33), fills)
    eng.evaluate(rt)
    adds = [i for i in ex.intents if i.signal is SignalType.SCALE_ADD]
    assert {a.side for a in adds} == {Side.YES, Side.NO}
    assert len({a.shares for a in adds}) == 1   # equal shares on both legs


def _late_window(rt):
    """Put the market inside the skew window (last 60s, above the 20s blackout)."""
    rt.market.end_ts = time.time() + 45


def test_skew_needs_confluence_and_late_window():
    chop = [100_000, 100_080, 99_930, 100_060, 100_010]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    # 1) confluence + conviction, but EARLY in the window (t_rem 180s) -> no skew
    _, _, rt0, ex0, eng0 = make_env(chop, 100_150, top(0.55, 0.57, 900, 100),
                                    top(0.43, 0.45, 100, 900), fills, drift=3e-6)
    eng0.evaluate(rt0)
    assert not [i for i in ex0.intents if i.signal is SignalType.SKEW]
    # 2) late window, model says YES but book imbalance is FLAT -> no skew
    _, _, rt, ex, eng = make_env(chop, 100_150, top(0.55, 0.57, 500, 500),
                                 top(0.43, 0.45, 500, 500), fills, drift=3e-6)
    _late_window(rt)
    eng.evaluate(rt)
    assert not [i for i in ex.intents if i.signal is SignalType.SKEW]
    # 3) late window + model conviction + book agreement -> skew fires
    _, _, rt2, ex2, eng2 = make_env(chop, 100_150, top(0.55, 0.57, 900, 100),
                                    top(0.43, 0.45, 100, 900), fills, drift=3e-6)
    _late_window(rt2)
    eng2.evaluate(rt2)
    skews = [i for i in ex2.intents if i.signal is SignalType.SKEW]
    assert skews and skews[0].side is Side.YES
    assert rt2.fair_yes >= 0.75          # the conviction floor really was met


def test_skew_blocked_below_conviction_floor():
    # late window, book agrees, but spot is barely past the strike: fair ~< 0.75
    chop = [100_000, 100_030, 99_980, 100_020, 100_010]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    _, _, rt, ex, eng = make_env(chop, 100_010, top(0.55, 0.57, 900, 100),
                                 top(0.43, 0.45, 100, 900), fills)
    _late_window(rt)
    eng.evaluate(rt)
    assert rt.fair_yes is None or rt.fair_yes < 0.75
    assert not [i for i in ex.intents if i.signal is SignalType.SKEW]


def test_regime_classifier_labels():
    trending = classify([100_000 + i * 50 for i in range(40)], 100_000, 2.0)
    assert trending.trending and trending.known
    choppy = classify([100_000, 100_100, 99_900, 100_080, 99_950, 100_010] * 6, 100_000, 2.0)
    assert not choppy.trending and choppy.known


def test_regime_fails_closed_without_enough_data():
    """Too few bars must freeze the ladder, not unlock it (the old code said choppy)."""
    starved = classify([100_000, 100_050], 100_000, 2.0)
    assert starved.trending is True      # trending => _scale_adds returns []
    assert starved.known is False
    assert starved.label == "unknown"


class PendingAwareExecutor(CollectingExecutor):
    """Simulates orders that never fill: open_shares reflects everything submitted."""

    def open_shares(self, market_id, side):
        return sum(i.shares for i in self.intents
                   if i.market_id == market_id and i.side is side
                   and i.action is Action.BUY)


def make_env_exec(spot_prices, spot_now, book_yes, book_no, position_fills=(),
                  drift=0.0, executor=None, start_offset=120):
    s = Settings(_env_file=None, dashboard_port=0, strategy="paired")
    hub = Hub()
    now = time.time()
    m = Market(condition_id="c1", slug="test", question="test?", asset="BTC",
               duration_s=300, start_ts=now - start_offset, end_ts=now + 180,
               token={Side.YES: "ty", Side.NO: "tn"}, strike=100_000.0)
    rt = MarketRuntime(market=m)
    rt.books[Side.YES] = book_yes
    rt.books[Side.NO] = book_no
    rt.position.base_placed = True
    for f in position_fills:
        rt.position.apply_fill(f)
    hub.markets["c1"] = rt
    ex = executor or CollectingExecutor()
    eng = SignalEngine(s, hub, {"BTC": FakeSpot(spot_now, spot_prices, drift=drift)},
                       PermissiveRisk(), ex, NullRecorder(), rng=random.Random(7))
    return s, hub, rt, ex, eng


def test_pending_order_blocks_duplicate_add():
    chop = [100_000, 100_080, 99_920, 100_070, 99_930, 100_005]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    ex = PendingAwareExecutor()
    _, _, rt, ex, eng = make_env_exec(chop, 100_000, top(0.60, 0.64), top(0.31, 0.33),
                                      fills, executor=ex)
    eng.evaluate(rt)
    n_first = len([i for i in ex.intents if i.signal is SignalType.SCALE_ADD])
    assert n_first == 2            # one pair = two legs
    rt.last_eval_ts = 0            # bypass throttle; conditions unchanged, orders pending
    eng.evaluate(rt)
    n_second = len([i for i in ex.intents if i.signal is SignalType.SCALE_ADD])
    assert n_second == 2           # no duplicate pair while the first is working


def test_skew_tp_churn_guard():
    chop = [100_000, 100_080, 99_930, 100_060, 100_010]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    # confluence + sane price, but TP already fired once -> churn guard blocks rebuy
    _, _, rt2, ex2, eng2 = make_env_exec(chop, 100_150, top(0.55, 0.57, 900, 100),
                                         top(0.43, 0.45, 100, 900), fills, drift=3e-6)
    rt2.market.end_ts = time.time() + 45   # inside the skew window
    rt2.position.tp_taken.add(0.90)
    eng2.evaluate(rt2)
    assert not [i for i in ex2.intents if i.signal is SignalType.SKEW]


def test_base_leg_repair_fires_once():
    chop = [100_000, 100_020, 99_990]
    # base placed; YES leg filled, NO leg died unfilled (bought NO = 0, nothing pending)
    fills = [buy(Side.YES, 0.50, 20)]
    _, _, rt, ex, eng = make_env_exec(chop, 100_000, top(0.48, 0.50), top(0.48, 0.50),
                                      fills, start_offset=40)
    eng.evaluate(rt)
    repairs = [i for i in ex.intents if i.signal is SignalType.BASE_ENTRY]
    assert len(repairs) == 1 and repairs[0].side is Side.NO
    rt.last_eval_ts = 0
    eng.evaluate(rt)               # retried flag set: no second repair
    assert len([i for i in ex.intents if i.signal is SignalType.BASE_ENTRY]) == 1


def test_window_age_gate_blocks_early_adds():
    chop = [100_000, 100_080, 99_920, 100_070, 99_930, 100_005]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    _, _, rt, ex, eng = make_env_exec(chop, 100_000, top(0.60, 0.64), top(0.31, 0.33),
                                      fills, start_offset=5)  # window only 5s old
    eng.evaluate(rt)
    assert not [i for i in ex.intents if i.signal is SignalType.SCALE_ADD]


def test_failed_order_backoff_blocks_refire():
    chop = [100_000, 100_080, 99_920, 100_070, 99_930, 100_005]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.50, 20)]
    _, hub, rt, ex, eng = make_env_exec(chop, 100_000, top(0.60, 0.64), top(0.31, 0.33), fills)
    n1 = len(eng.evaluate(rt))
    assert n1 == 2                      # the pair fires (both legs)
    intent = ex.intents[-1]
    hub.order_closed(intent, 0.0)       # one leg died unfilled -> that side backs off
    rt.last_eval_ts = 0
    assert eng.evaluate(rt) == []       # a blocked side blocks the whole pair
    rt.position.blocked_until[intent.side] = 0.0   # backoff expires
    rt.last_eval_ts = 0
    assert len(eng.evaluate(rt)) == 2   # the pair may retry


def test_min_notional_sizes_the_pair_together():
    # cheap leg at 0.12: 5 shares = $0.60 < $1 minimum -> BOTH legs sized up equally
    # (0.84 + 0.12 = 0.96 <= PAIR_ADD_MAX so the pair triggers)
    chop = [100_000, 100_080, 99_920, 100_070, 99_930, 100_005]
    fills = [buy(Side.YES, 0.50, 20), buy(Side.NO, 0.30, 20)]
    _, _, rt, ex, eng = make_env_exec(chop, 100_000, top(0.82, 0.84), top(0.10, 0.12), fills)
    eng.evaluate(rt)
    adds = [i for i in ex.intents if i.signal is SignalType.SCALE_ADD]
    assert adds and len({a.shares for a in adds}) == 1   # legs stay equal
    for i in adds:
        assert i.price * i.shares >= 1.0, (i.signal, i.price, i.shares)
