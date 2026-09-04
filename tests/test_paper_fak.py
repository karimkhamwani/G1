"""Paper mode must mirror live FAK semantics: fill what's visible, kill the rest."""
import time

from bot.execution.paper import PaperExecutor
from bot.models import Action, BookTop, Market, OrderIntent, Side, SignalType
from bot.settings import Settings
from bot.state import Hub, MarketRuntime


class NullRecorder:
    def log(self, *a, **k):
        pass


class FakeSpot:
    price = 100_000.0
    stale = 0.0


def make_paper(ask=0.50, ask_size=7.0, order_type="FAK"):
    s = Settings(_env_file=None, dashboard_port=0, paper_latency_ms=0, buy_order_type=order_type)
    hub = Hub()
    now = time.time()
    m = Market(condition_id="c1", slug="t", question="t?", asset="BTC", duration_s=300,
               start_ts=now - 60, end_ts=now + 240,
               token={Side.YES: "ty", Side.NO: "tn"}, strike=100_000.0)
    rt = MarketRuntime(market=m)
    rt.books[Side.YES] = BookTop(bid=ask - 0.02, bid_size=100, ask=ask, ask_size=ask_size,
                                 bid_depth_usdc=500, ask_depth_usdc=500, ts=now)
    rt.books[Side.NO] = BookTop(bid=0.46, bid_size=100, ask=0.48, ask_size=100,
                                bid_depth_usdc=500, ask_depth_usdc=500, ts=now)
    hub.markets["c1"] = rt
    ex = PaperExecutor(s, hub, {"BTC": FakeSpot()}, NullRecorder())
    return s, hub, rt, ex


def test_fak_partial_fill_kills_remainder():
    s, hub, rt, ex = make_paper(ask=0.50, ask_size=7.0)
    intent = OrderIntent(market_id="c1", token_id="ty", side=Side.YES, action=Action.BUY,
                         price=0.50, shares=10.0, signal=SignalType.SKEW)
    ex.submit(intent)
    ex._activate(time.time() + 1)
    # filled only the visible 7, remainder killed — nothing rests
    assert abs(rt.position.shares[Side.YES] - 7.0) < 1e-9
    assert ex.open_shares("c1", Side.YES) == 0.0


def test_fak_cross_buffer_keeps_moved_ask_marketable():
    # ask moved 2 ticks above the intent price during latency: buffer still fills it
    s, hub, rt, ex = make_paper(ask=0.52, ask_size=100.0)
    intent = OrderIntent(market_id="c1", token_id="ty", side=Side.YES, action=Action.BUY,
                         price=0.50, shares=10.0, signal=SignalType.SKEW)
    ex.submit(intent)
    ex._activate(time.time() + 1)
    assert abs(rt.position.shares[Side.YES] - 10.0) < 1e-9
    # but 3 ticks away is beyond the buffer: no fill, order dies
    s2, hub2, rt2, ex2 = make_paper(ask=0.53, ask_size=100.0)
    intent2 = OrderIntent(market_id="c1", token_id="ty", side=Side.YES, action=Action.BUY,
                          price=0.50, shares=10.0, signal=SignalType.SKEW)
    ex2.submit(intent2)
    ex2._activate(time.time() + 1)
    assert rt2.position.shares[Side.YES] == 0.0
    assert ex2.open_shares("c1", Side.YES) == 0.0


def test_fak_no_fill_rearms_tp_level():
    s, hub, rt, ex = make_paper(ask=0.53, ask_size=100.0)   # beyond buffer -> no fill
    intent = OrderIntent(market_id="c1", token_id="ty", side=Side.YES, action=Action.SELL,
                         price=0.90, shares=10.0, signal=SignalType.TAKE_PROFIT)
    rt.books[Side.YES] = BookTop(bid=None, bid_size=0, ask=0.53, ask_size=100,
                                 bid_depth_usdc=0, ask_depth_usdc=500, ts=time.time())
    rt.position.tp_taken.add(0.90)
    rt.position.tp_pending[intent.id] = 0.90
    ex.submit(intent)
    ex._activate(time.time() + 1)   # no bid: sell dies -> level re-armed
    assert 0.90 not in rt.position.tp_taken


def test_fok_is_all_or_nothing():
    # visible 7 < 10 wanted: FOK fills NOTHING (FAK would take the 7)
    s, hub, rt, ex = make_paper(ask=0.50, ask_size=7.0, order_type="FOK")
    intent = OrderIntent(market_id="c1", token_id="ty", side=Side.YES, action=Action.BUY,
                         price=0.50, shares=10.0, signal=SignalType.DIRECTIONAL)
    ex.submit(intent)
    ex._activate(time.time() + 1)
    assert rt.position.shares[Side.YES] == 0.0
    assert ex.open_shares("c1", Side.YES) == 0.0          # killed, nothing resting
    # enough visible size: fills the whole order
    s2, hub2, rt2, ex2 = make_paper(ask=0.50, ask_size=12.0, order_type="FOK")
    intent2 = OrderIntent(market_id="c1", token_id="ty", side=Side.YES, action=Action.BUY,
                          price=0.50, shares=10.0, signal=SignalType.DIRECTIONAL)
    ex2.submit(intent2)
    ex2._activate(time.time() + 1)
    assert abs(rt2.position.shares[Side.YES] - 10.0) < 1e-9
