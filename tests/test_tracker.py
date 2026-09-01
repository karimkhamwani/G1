from bot.execution.tracker import Position
from bot.models import Action, Fill, Side, SignalType


def buy(side, price, shares, signal=SignalType.BASE_ENTRY, fee=0.0):
    return Fill(market_id="m", side=side, action=Action.BUY, price=price,
                shares=shares, fee=fee, signal=signal)


def test_averaging_and_matched():
    p = Position()
    p.apply_fill(buy(Side.YES, 0.50, 20))
    p.apply_fill(buy(Side.NO, 0.50, 20))
    p.apply_fill(buy(Side.NO, 0.30, 20, SignalType.SCALE_ADD))
    assert p.avg(Side.YES) == 0.50
    assert abs(p.avg(Side.NO) - 0.40) < 1e-9
    assert abs(p.combined_avg - 0.90) < 1e-9
    assert p.matched == 20
    assert p.skew_side is Side.NO and p.skew_shares == 20
    assert p.adds_used[Side.NO] == 1


def test_resolution_pnl_both_outcomes():
    p = Position()
    p.apply_fill(buy(Side.YES, 0.50, 10))
    p.apply_fill(buy(Side.NO, 0.50, 10))
    p.apply_fill(buy(Side.NO, 0.30, 10, SignalType.SCALE_ADD))
    # cost = 5 + 5 + 3 = 13; YES wins -> payout 10; NO wins -> payout 20
    assert abs(p.resolution_pnl(Side.YES) - (10 - 13)) < 1e-9
    assert abs(p.resolution_pnl(Side.NO) - (20 - 13)) < 1e-9


def test_sell_realizes_pnl():
    p = Position()
    p.apply_fill(buy(Side.YES, 0.50, 20, SignalType.SKEW))
    sell = Fill(market_id="m", side=Side.YES, action=Action.SELL, price=0.90,
                shares=10, fee=0.0, signal=SignalType.TAKE_PROFIT)
    p.apply_fill(sell)
    assert abs(p.realized - 4.0) < 1e-9        # (0.90-0.50)*10
    assert p.shares[Side.YES] == 10
    assert abs(p.cost[Side.YES] - 5.0) < 1e-9  # remaining at avg 0.50


def test_layer_attribution_with_sell_sums_to_total():
    p = Position()
    p.apply_fill(buy(Side.YES, 0.50, 20, SignalType.SKEW))
    sell = Fill(market_id="m", side=Side.YES, action=Action.SELL, price=0.90,
                shares=10, fee=0.0, signal=SignalType.TAKE_PROFIT)
    p.apply_fill(sell)
    # spent 10, sold 10sh @0.90 = 9, YES wins pays remaining 10sh -> total +9
    assert abs(p.resolution_pnl(Side.YES) - 9.0) < 1e-9
    layers = p.layer_pnl(Side.YES)
    assert abs(layers[2] - 9.0) < 1e-9
    assert abs(sum(layers.values()) - p.resolution_pnl(Side.YES)) < 1e-9


def test_layer_attribution():
    p = Position()
    p.apply_fill(buy(Side.YES, 0.50, 10))                       # layer 1
    p.apply_fill(buy(Side.NO, 0.50, 10))                        # layer 1
    p.apply_fill(buy(Side.YES, 0.60, 10, SignalType.SKEW))      # layer 2
    layers = p.layer_pnl(Side.YES)
    assert abs(layers[1] - (10 - 10.0)) < 1e-9   # matched base: payout 10, cost 10
    assert abs(layers[2] - (10 - 6.0)) < 1e-9    # skew: payout 10, cost 6
    layers_no = p.layer_pnl(Side.NO)
    assert abs(layers_no[2] - (0 - 6.0)) < 1e-9  # skew loses whole cost
