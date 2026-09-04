"""Directional strategy: two conditions, $10 budget, threshold-held adds, FOK."""
import random
import time

from bot.models import Action, BookTop, Fill, Market, Side, SignalType
from bot.settings import Settings
from bot.signal.engine import SignalEngine
from bot.state import Hub, MarketRuntime
from tests.test_engine_gates import (CollectingExecutor, FakeSpot, NullRecorder,
                                     PermissiveRisk, top)

CHOP = [100_000, 100_080, 99_920, 100_070, 99_930, 100_005]


def make_dir(spot_now, book_yes, book_no, fills=(), **overrides):
    s = Settings(_env_file=None, dashboard_port=0, strategy="directional", **overrides)
    hub = Hub()
    now = time.time()
    m = Market(condition_id="c1", slug="t", question="t?", asset="BTC", duration_s=300,
               start_ts=now - 120, end_ts=now + 180,
               token={Side.YES: "ty", Side.NO: "tn"}, strike=100_000.0)
    rt = MarketRuntime(market=m)
    rt.books[Side.YES], rt.books[Side.NO] = book_yes, book_no
    for f in fills:
        rt.position.apply_fill(f)
    hub.markets["c1"] = rt
    ex = CollectingExecutor()
    spot = FakeSpot(spot_now, CHOP)
    eng = SignalEngine(s, hub, {"BTC": spot}, PermissiveRisk(), ex, NullRecorder(),
                       rng=random.Random(3))
    return s, hub, rt, ex, eng, spot


def dir_fills(ex):
    return [i for i in ex.intents if i.signal is SignalType.DIRECTIONAL]


def test_first_bet_when_both_conditions_hold():
    # spot +0.15% with 180s left -> fair_yes ~0.87 >= 0.75; YES ask 0.74 >= 0.70 (book agrees)
    _, _, rt, ex, eng, _ = make_dir(100_150, top(0.72, 0.74), top(0.24, 0.26))
    eng.evaluate(rt)
    bets = dir_fills(ex)
    assert rt.fair_yes >= 0.75
    assert len(bets) == 1 and bets[0].side is Side.YES and bets[0].shares == 5.0
    assert bets[0].price == 0.74


def test_no_bet_if_either_condition_fails():
    # model confident but the book does NOT agree yet (ask 0.62 < 0.70)
    _, _, rt, ex, eng, _ = make_dir(100_150, top(0.60, 0.62), top(0.36, 0.38))
    eng.evaluate(rt)
    assert dir_fills(ex) == []


def test_book_ahead_of_model_still_enters():
    # the BOOK crossed first (ask 0.92) and the model is behind it (~0.87) but above
    # its own 0.75 threshold: both thresholds met -> enter, order of crossing irrelevant
    _, _, rt, ex, eng, _ = make_dir(100_150, top(0.90, 0.92), top(0.06, 0.08))
    eng.evaluate(rt)
    assert 0.75 <= rt.fair_yes < 0.92
    assert len(dir_fills(ex)) == 1
    # the optional edge gate, when switched on, refuses to pay above the model's fair
    _, _, rt2, ex2, eng2, _ = make_dir(100_150, top(0.90, 0.92), top(0.06, 0.08),
                                       dir_require_edge=True)
    eng2.evaluate(rt2)
    assert dir_fills(ex2) == []
    # cheap book but no confidence (spot barely moved -> fair ~0.5)
    _, _, rt2, ex2, eng2, _ = make_dir(100_005, top(0.48, 0.50), top(0.48, 0.50))
    eng2.evaluate(rt2)
    assert rt2.fair_yes < 0.75 and dir_fills(ex2) == []


def test_more_bets_only_when_threshold_held_and_confidence_rises():
    _, hub, rt, ex, eng, spot = make_dir(100_150, top(0.72, 0.74), top(0.24, 0.26))
    eng.evaluate(rt)
    first = dir_fills(ex)[0]
    # the bet fills at the current confidence
    hub.on_fill(Fill("c1", Side.YES, Action.BUY, first.price, first.shares, 0.0,
                     SignalType.DIRECTIONAL, order_id=first.id))
    conf_at_fill = rt.position.dir_last_fair
    assert conf_at_fill >= 0.75
    # same confidence -> no second bet
    rt.last_eval_ts = 0
    eng.evaluate(rt)
    assert len(dir_fills(ex)) == 1
    # confidence rises (spot moves further) -> next bet allowed
    spot.price = 100_260
    rt.last_eval_ts = 0
    eng.evaluate(rt)
    assert len(dir_fills(ex)) == 2
    assert rt.fair_yes >= conf_at_fill + 0.02


def test_budget_hard_cap_10_dollars():
    # already spent $9.30 (15 shares @ 0.62): only $0.70 left -> no valid 5-share bet
    fills = [Fill("c1", Side.YES, Action.BUY, 0.62, 15, 0.0, SignalType.DIRECTIONAL)]
    _, _, rt, ex, eng, _ = make_dir(100_300, top(0.72, 0.74), top(0.24, 0.26), fills)
    rt.position.dir_last_fair = 0.5   # confidence has "risen" plenty since
    eng.evaluate(rt)
    assert dir_fills(ex) == []
    # with $6.20 spent there is room for exactly one more 5-share bet ($3.70 -> $9.90)
    fills2 = [Fill("c1", Side.YES, Action.BUY, 0.62, 10, 0.0, SignalType.DIRECTIONAL)]
    _, _, rt2, ex2, eng2, _ = make_dir(100_300, top(0.72, 0.74), top(0.24, 0.26), fills2)
    rt2.position.dir_last_fair = 0.5
    eng2.evaluate(rt2)
    bets = dir_fills(ex2)
    assert len(bets) == 1
    assert rt2.position.cost_basis + bets[0].price * bets[0].shares <= 10.0


def test_never_bets_against_an_existing_position():
    # we hold NO, but the model now backs YES: no bet (that would build a pair)
    fills = [Fill("c1", Side.NO, Action.BUY, 0.40, 5, 0.0, SignalType.DIRECTIONAL)]
    _, _, rt, ex, eng, _ = make_dir(100_150, top(0.72, 0.74), top(0.24, 0.26), fills)
    eng.evaluate(rt)
    assert dir_fills(ex) == []


def test_stop_loss_sells_all_when_book_and_model_collapse():
    # we hold 5 YES @0.74; spot fell below strike -> fair_yes ~0.13; YES ask 0.40 <= 0.45
    fills = [Fill("c1", Side.YES, Action.BUY, 0.74, 5, 0.0, SignalType.DIRECTIONAL)]
    _, _, rt, ex, eng, _ = make_dir(99_850, top(0.38, 0.40), top(0.58, 0.60), fills)
    eng.evaluate(rt)
    stops = [i for i in ex.intents if i.signal is SignalType.STOP_LOSS]
    assert rt.fair_yes <= 0.45
    assert len(stops) == 1 and stops[0].action is Action.SELL and stops[0].side is Side.YES
    assert stops[0].shares == 5.0 and stops[0].price == 0.38     # everything, at the bid
    assert rt.position.exit_pending
    # once it fills, the market is marked stopped-out: no re-entry even if thresholds return
    hub_fill = Fill("c1", Side.YES, Action.SELL, 0.38, 5, 0.0, SignalType.STOP_LOSS,
                    order_id=stops[0].id)
    rt.position.apply_fill(hub_fill)
    assert rt.position.stopped_out and rt.position.shares[Side.YES] == 0.0
    rt.books[Side.YES] = top(0.72, 0.74); rt.books[Side.NO] = top(0.24, 0.26)
    eng.spots["BTC"].price = 100_150
    rt.last_eval_ts = 0
    eng.evaluate(rt)
    assert dir_fills(ex) == []


def test_stop_loss_needs_both_book_and_model_below():
    fills = [Fill("c1", Side.YES, Action.BUY, 0.74, 5, 0.0, SignalType.DIRECTIONAL)]
    # book collapsed (ask 0.40) but the model still likes it (spot up -> fair ~0.87): hold
    _, _, rt, ex, eng, _ = make_dir(100_150, top(0.38, 0.40), top(0.58, 0.60), fills)
    eng.evaluate(rt)
    assert not [i for i in ex.intents if i.signal is SignalType.STOP_LOSS]
    # model collapsed (fair ~0.13) but the book still prices YES at 0.60: hold
    _, _, rt2, ex2, eng2, _ = make_dir(99_850, top(0.58, 0.60), top(0.38, 0.40), fills)
    eng2.evaluate(rt2)
    assert not [i for i in ex2.intents if i.signal is SignalType.STOP_LOSS]


def test_stop_loss_retries_if_sell_dies():
    fills = [Fill("c1", Side.YES, Action.BUY, 0.74, 5, 0.0, SignalType.DIRECTIONAL)]
    _, hub, rt, ex, eng, _ = make_dir(99_850, top(0.38, 0.40), top(0.58, 0.60), fills)
    eng.evaluate(rt)
    first = [i for i in ex.intents if i.signal is SignalType.STOP_LOSS][0]
    hub.order_closed(first, 0.0)          # sell died unfilled -> re-armed
    assert not rt.position.exit_pending
    rt.position.blocked_until[Side.YES] = 0.0
    rt.last_eval_ts = 0
    eng.evaluate(rt)
    assert len([i for i in ex.intents if i.signal is SignalType.STOP_LOSS]) == 2
