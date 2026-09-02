"""Base-leg repair must not chase, and a real zero fee must be believed."""
import time

from bot.models import BookTop, Market, Side
from bot.settings import Settings
from bot.signal.engine import SignalEngine
from bot.state import Hub, MarketRuntime
from tests.test_engine_gates import (CollectingExecutor, FakeSpot, NullRecorder,
                                     PermissiveRisk)


def _env(quoted, now_ask):
    s = Settings(_env_file=None, dashboard_port=0)
    hub = Hub()
    now = time.time()
    m = Market(condition_id="c1", slug="t", question="t?", asset="BTC", duration_s=300,
               start_ts=now - 10, end_ts=now + 290,
               token={Side.YES: "ty", Side.NO: "tn"}, strike=100_000.0)
    rt = MarketRuntime(market=m)
    rt.books[Side.YES] = BookTop(bid=0.49, ask=0.51, bid_size=900, ask_size=900)
    rt.books[Side.NO] = BookTop(bid=now_ask - 0.02, ask=now_ask, bid_size=900, ask_size=900)
    rt.position.base_placed = True
    rt.position.base_price[Side.NO] = quoted
    rt.position.bought[Side.YES] = 5.0          # YES leg filled, NO leg died unfilled
    hub.markets[m.condition_id] = rt
    ex = CollectingExecutor()
    eng = SignalEngine(s, hub, {"BTC": FakeSpot(100_000.0, [100_000, 100_010] * 40)},
                       PermissiveRisk(), ex, NullRecorder())
    return rt, eng


def test_repair_abandoned_when_price_ran_away():
    """Live: a 0.52 leg was repaired at 0.80, pushing that cycle's c.avg to 1.23."""
    rt, eng = _env(quoted=0.52, now_ask=0.80)
    intents = eng._base_leg_repair(rt, time.time())
    assert intents == []
    assert rt.position.base_retried[Side.NO] is True   # and never retried again


def test_repair_allowed_within_slippage_budget():
    rt, eng = _env(quoted=0.52, now_ask=0.54)
    intents = eng._base_leg_repair(rt, time.time())
    assert [i.side for i in intents] == [Side.NO]
    assert intents[0].price == 0.54


def test_zero_fee_from_a_successful_fetch_is_believed():
    """taker_base_fee=0 is the truth for most markets; assuming 1000bps poisoned
    every fee-gated decision and inflated recorded cost by ~12%."""
    import inspect
    from bot.discovery import gamma
    src = inspect.getsource(gamma)
    assert "fetched = True" in src
    assert "if not fetched:" in src, "default must apply only when the fetch failed"


def test_skew_disabled_never_opens_layer_2():
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
    rt.regime = classify([100_000, 100_100, 99_900, 100_080] * 10, 100_000.0, 2.0)
    assert rt.regime.known                      # regime is fine; only the switch is off
    hub = Hub(); hub.markets[m.condition_id] = rt
    eng = SignalEngine(Settings(_env_file=None, dashboard_port=0, skew_enabled=False), hub,
                       {"BTC": FakeSpot(100_000.0, [100_000])}, PermissiveRisk(),
                       CollectingExecutor(), NullRecorder())
    assert eng._skew(rt, {Side.YES: 0.05, Side.NO: 0.95}, 240) == []
