"""Per-market position accounting: per-side volume-weighted averages, matched vs skew."""
from __future__ import annotations

from dataclasses import dataclass, field

from bot.models import Fill, Side, SignalType, LAYER_OF


@dataclass
class Position:
    shares: dict[Side, float] = field(default_factory=lambda: {Side.YES: 0.0, Side.NO: 0.0})
    cost: dict[Side, float] = field(default_factory=lambda: {Side.YES: 0.0, Side.NO: 0.0})
    adds_used: dict[Side, int] = field(default_factory=lambda: {Side.YES: 0, Side.NO: 0})
    ordered: dict[Side, float] = field(default_factory=lambda: {Side.YES: 0.0, Side.NO: 0.0})
    bought: dict[Side, float] = field(default_factory=lambda: {Side.YES: 0.0, Side.NO: 0.0})
    fill_count: dict[Side, int] = field(default_factory=lambda: {Side.YES: 0, Side.NO: 0})
    skew_bought: float = 0.0                    # total layer-2 shares bought (gates the cap)
    skew_by_side: dict[Side, float] = field(default_factory=lambda: {Side.YES: 0.0, Side.NO: 0.0})
    tp_taken: set[float] = field(default_factory=set)
    tp_pending: dict[int, float] = field(default_factory=dict)  # intent.id -> level, until outcome known
    base_placed: bool = False
    base_retried: dict[Side, bool] = field(default_factory=lambda: {Side.YES: False, Side.NO: False})
    realized: float = 0.0                       # from sells before resolution
    fees_paid: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    # ---- fill application ---------------------------------------------
    def apply_fill(self, f: Fill) -> None:
        if f.action.value == "BUY":
            self.shares[f.side] += f.shares
            self.cost[f.side] += f.price * f.shares + f.fee
            self.bought[f.side] += f.shares
            self.fill_count[f.side] += 1
            if f.signal is SignalType.SCALE_ADD:
                self.adds_used[f.side] += 1
            if f.signal is SignalType.SKEW:
                self.skew_bought += f.shares
                self.skew_by_side[f.side] += f.shares
        else:  # SELL — reduce at average cost, book the difference as realized
            avg = self.avg(f.side) or 0.0
            sell = min(f.shares, self.shares[f.side])
            self.realized += (f.price - avg) * sell - f.fee
            self.shares[f.side] -= sell
            self.cost[f.side] -= avg * sell
            if f.signal is SignalType.TAKE_PROFIT and sell > 0:
                # a filled TP settles its pending level as genuinely taken
                self.tp_pending.pop(f.order_id, None)
        self.fees_paid += f.fee
        self.fills.append(f)

    def order_closed(self, intent, filled_shares: float) -> None:
        """An order died at the executor (cancel/TTL/reject/drop). Re-arm one-shot
        state that was consumed at intent time but never produced a fill."""
        if intent.signal is SignalType.TAKE_PROFIT and filled_shares < 0.5:
            level = self.tp_pending.pop(intent.id, None)
            if level is not None:
                self.tp_taken.discard(level)

    # ---- derived views -------------------------------------------------
    def fill_rate(self, side: Side) -> float | None:
        """Share of ordered volume that actually filled (None before any order)."""
        return self.bought[side] / self.ordered[side] if self.ordered[side] > 0 else None

    def avg(self, side: Side) -> float | None:
        if self.shares[side] <= 0:
            return None
        return self.cost[side] / self.shares[side]

    @property
    def combined_avg(self) -> float | None:
        a, b = self.avg(Side.YES), self.avg(Side.NO)
        if a is None or b is None:
            return None
        return a + b

    @property
    def matched(self) -> float:
        return min(self.shares[Side.YES], self.shares[Side.NO])

    @property
    def skew_l2_side(self) -> Side | None:
        """Side layer 2 actually bought (None if it never fired)."""
        y, n = self.skew_by_side[Side.YES], self.skew_by_side[Side.NO]
        if y > n:
            return Side.YES
        if n > y:
            return Side.NO
        return None

    @property
    def skew_side(self) -> Side | None:
        if self.shares[Side.YES] > self.shares[Side.NO]:
            return Side.YES
        if self.shares[Side.NO] > self.shares[Side.YES]:
            return Side.NO
        return None

    @property
    def skew_shares(self) -> float:
        """NET directional imbalance from all layers — not layer-2 buying (see skew_bought)."""
        return abs(self.shares[Side.YES] - self.shares[Side.NO])

    @property
    def total_shares(self) -> float:
        return self.shares[Side.YES] + self.shares[Side.NO]

    @property
    def cost_basis(self) -> float:
        return self.cost[Side.YES] + self.cost[Side.NO]

    def unrealized(self, mid: dict[Side, float | None]) -> float | None:
        """Mark-to-mid P&L of open shares."""
        total = 0.0
        for s in Side:
            m = mid.get(s)
            if self.shares[s] > 0:
                if m is None:
                    return None
                total += self.shares[s] * m - self.cost[s]
        return total + self.realized

    def resolution_pnl(self, winner: Side) -> float:
        """Realized P&L if the market resolves to `winner` right now."""
        payout = self.shares[winner] * 1.0
        return payout - self.cost_basis + self.realized

    def layer_pnl(self, winner: Side) -> dict[int, float]:
        """Attribute resolution P&L to layer 1 (base+ladder) vs layer 2 (skew+TP).

        Chronological walk over fills. A SELL consumes the selling layer's own
        inventory first, then spills into the other layer's (a TP can sell layer-1
        shares when the net imbalance includes them) — each portion's realized P&L is
        booked to the layer whose inventory was sold. The layer sums always equal
        resolution_pnl exactly.
        """
        shares = {1: {s: 0.0 for s in Side}, 2: {s: 0.0 for s in Side}}
        cost = {1: {s: 0.0 for s in Side}, 2: {s: 0.0 for s in Side}}
        realized = {1: 0.0, 2: 0.0}
        for f in self.fills:
            layer = LAYER_OF[f.signal]
            if f.action.value == "BUY":
                shares[layer][f.side] += f.shares
                cost[layer][f.side] += f.price * f.shares + f.fee
                continue
            qty = f.shares
            fee_per_share = f.fee / f.shares if f.shares else 0.0
            for lay in (layer, 1 if layer == 2 else 2):
                take = min(qty, shares[lay][f.side])
                if take <= 0:
                    continue
                avg = cost[lay][f.side] / shares[lay][f.side]
                realized[lay] += (f.price - avg) * take - fee_per_share * take
                shares[lay][f.side] -= take
                cost[lay][f.side] -= avg * take
                qty -= take
                if qty <= 1e-9:
                    break
        return {
            lay: shares[lay][winner] - (cost[lay][Side.YES] + cost[lay][Side.NO]) + realized[lay]
            for lay in (1, 2)
        }
