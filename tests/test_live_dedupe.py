"""The user channel repeats each trade as its status advances; book it once."""
from bot.execution.live import LiveExecutor
from bot.models import Action, Order, OrderIntent, OrderStatus, Side, SignalType


class _Rec:
    def __init__(self): self.events = []
    def log(self, t, p): self.events.append((t, p))


class _Hub:
    def __init__(self): self.fills = []; self.markets = {}
    def on_fill(self, f, latency_ms=None): self.fills.append(f); return {"shares": f.shares}


def _executor():
    """LiveExecutor without __init__ (which would need a wallet key + network)."""
    ex = LiveExecutor.__new__(LiveExecutor)
    ex._seen_trades = __import__("collections").OrderedDict()
    ex.recorder, ex.hub, ex.spots = _Rec(), _Hub(), {}
    intent = OrderIntent(market_id="m", token_id="t", side=Side.YES, action=Action.BUY,
                         price=0.64, shares=5.0, signal=SignalType.BASE_ENTRY)
    ex.orders = {"OID1": Order(intent=intent, status=OrderStatus.RESTING, exchange_id="OID1")}
    return ex


def _trade(status, tid="TRADE-1"):
    return {"event_type": "trade", "id": tid, "status": status,
            "taker_order_id": "OID1", "price": "0.64", "size": "5.0"}


def test_status_resends_book_one_fill():
    ex = _executor()
    for status in ("MATCHED", "MINED", "CONFIRMED"):   # the +2.2s / +6.1s re-sends
        ex._on_trade_event(_trade(status))
    assert len(ex.hub.fills) == 1
    assert ex.hub.fills[0].shares == 5.0
    assert sum(1 for t, _ in ex.recorder.events if t == "trade_duplicate") == 2


def test_distinct_trades_both_book():
    ex = _executor()
    ex._on_trade_event(_trade("MATCHED", "TRADE-1"))
    ex._on_trade_event(_trade("MATCHED", "TRADE-2"))   # genuine second partial fill
    assert len(ex.hub.fills) == 2


def test_failed_trade_never_books():
    ex = _executor()
    ex._on_trade_event(_trade("FAILED"))
    assert ex.hub.fills == []


def test_seen_trades_stays_bounded():
    ex = _executor()
    for n in range(LiveExecutor.SEEN_TRADES_MAX + 250):
        ex._on_trade_event(_trade("MATCHED", f"T{n}"))
    assert len(ex._seen_trades) <= LiveExecutor.SEEN_TRADES_MAX
