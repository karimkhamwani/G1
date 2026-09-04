"""Backtest: replay recorded JSONL through the SAME SignalEngine + a simulated executor.

Usage:
    python -m bot.backtest.replay data/2026-09-01 [data/2026-09-02 ...]

Fill model: identical rules to paper mode (marketable at ask capped at size; resting
buys fill only on trade-through), with the configured latency applied as event-time
delay. Reports the outputs plan.md §3.7 requires — P&L by regime and layer, achieved
combined average cost, fill-rate calibration, expectancy, and the ROI arithmetic.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from bot.execution.tracker import Position
from bot.models import Action, BookTop, Fill, Market, OrderIntent, Side, SignalType
from bot.settings import Settings
from bot.signal.engine import SignalEngine
from bot.state import Hub, MarketRuntime


class ReplaySpotState:
    """SpotState fed from recorded spot_bar events (sigma/drift were recorded)."""

    def __init__(self, vol_window_s: int) -> None:
        self.price: float | None = None
        self.ts: float = 0.0
        self.sigma: float = 0.0
        self.drift: float = 0.0
        self.bars: list[tuple[float, float]] = []

    def on_bar(self, ev: dict) -> None:
        self.price = ev["price"]
        self.ts = ev["ts"]
        self.sigma = ev.get("sigma", 0.0)
        self.drift = ev.get("drift", 0.0)
        self.bars.append((ev["bar_ts"], ev["price"]))
        if len(self.bars) > 7200:
            self.bars = self.bars[-3600:]

    def sigma_per_sqrt_s(self) -> float:
        return self.sigma

    def drift_per_s(self) -> float:
        return self.drift

    def prices_since(self, since_ts: float) -> list[float]:
        return [p for t, p in self.bars if t >= since_ts]

    @property
    def stale(self) -> float:
        return 0.0


class SimExecutor:
    """Event-time fill simulator with the paper-mode rules."""

    def __init__(self, settings, hub, spots) -> None:
        self.s = settings
        self.hub = hub
        self.spots = spots
        self.resting: list[dict] = []
        self.now = 0.0
        self.submitted = 0
        self.filled_orders = 0

    def submit(self, intent: OrderIntent) -> None:
        self.submitted += 1
        self.resting.append({"i": intent, "activate": self.now + self.s.paper_latency_ms / 1000.0,
                             "placed": self.now, "active": False, "filled": 0.0,
                             "spot": self.spots[self.hub.markets[intent.market_id].market.asset].price})

    def _drop(self, r: dict) -> None:
        """Remove a working order and notify the hub (re-arms one-shot state like TP levels)."""
        if r in self.resting:
            self.resting.remove(r)
        self.hub.order_closed(r["i"], r["filled"], now=self.now)

    def cancel_market(self, market_id: str, why: str = "") -> int:
        doomed = [r for r in self.resting if r["i"].market_id == market_id]
        for r in doomed:
            self._drop(r)
        return len(doomed)

    def cancel_all(self, why: str = "") -> int:
        doomed = list(self.resting)
        for r in doomed:
            self._drop(r)
        return len(doomed)

    def open_shares(self, market_id: str, side) -> float:
        return sum(r["i"].shares - r["filled"] for r in self.resting
                   if r["i"].market_id == market_id and r["i"].side is side
                   and r["i"].action is Action.BUY)

    def open_buy_orders(self) -> list[tuple[str, object, float, float]]:
        return [(r["i"].market_id, r["i"].side, r["i"].shares - r["filled"], r["i"].price)
                for r in self.resting if r["i"].action is Action.BUY]

    def on_trade_print(self, token_id: str, price: float) -> None:
        for r in list(self.resting):
            i = r["i"]
            if r["active"] and i.token_id == token_id and i.action is Action.BUY and price < i.price:
                self._fill(r, i.price)

    def tick(self, now: float) -> None:
        self.now = now
        for r in list(self.resting):
            i = r["i"]
            rt = self.hub.markets.get(i.market_id)
            if rt is None or rt.resolved:
                self._drop(r)
                continue
            top = rt.books[i.side]
            if not r["active"]:
                if now < r["activate"]:
                    continue
                r["active"] = True
                if i.action is Action.SELL:
                    if top.bid is not None:
                        self._fill(r, top.bid, cap=top.bid_size)
                    self._drop(r)   # sells don't rest (same as paper mode)
                    continue
                # BUY: FOK/FAK semantics, same as paper/live (no resting buys)
                limit = min(0.99, i.price + self.s.order_cross_ticks * 0.01)
                if top.ask is not None and top.ask <= limit:
                    if self.s.buy_order_type == "FOK":
                        if top.ask_size >= i.shares:
                            self._fill(r, top.ask)
                    else:
                        self._fill(r, top.ask, cap=top.ask_size)
                self._drop(r)
                continue
            if now - r["placed"] > self.s.order_ttl_s:
                self._drop(r)

    def _fill(self, r: dict, price: float, cap: float | None = None) -> None:
        i = r["i"]
        remaining = i.shares - r["filled"]
        shares = min(remaining, cap) if cap is not None else remaining  # no fill floor
        if shares <= 0:
            return
        self.hub.on_fill(Fill(market_id=i.market_id, side=i.side, action=i.action,
                              price=price, shares=shares, fee=0.0, signal=i.signal,
                              ts=self.now, order_id=i.id))
        self.filled_orders += 1
        r["filled"] += shares
        if r["filled"] >= i.shares - 0.5 and r in self.resting:
            self.resting.remove(r)


class NullRecorder:
    def log(self, *a, **k) -> None:
        pass


class NullRisk:
    def __init__(self, settings, hub):
        from bot.risk.manager import RiskManager
        self._rm = RiskManager(settings, hub, NullRecorder())
        self._rm.check_staleness = False  # replaying historical timestamps

    def validate(self, intent, rt):
        return self._rm.validate(intent, rt)


def run(paths: list[Path]) -> dict:
    s = Settings(dashboard_port=0)
    hub = Hub()
    spots: dict[str, ReplaySpotState] = defaultdict(lambda: ReplaySpotState(s.vol_window_min * 60))
    sim = SimExecutor(s, hub, spots)
    risk = NullRisk(s, hub)
    risk._rm.executor = sim
    clock = {"now": 0.0}
    engine = SignalEngine(s, hub, spots, risk, sim, NullRecorder(),
                          clock=lambda: clock["now"], rng=random.Random(1))  # deterministic replays

    results = []

    def settle(rt, winner: Side, source: str) -> None:
        rt.resolved = True
        sim.cancel_market(rt.market.condition_id)
        pos = rt.position
        if pos.total_shares > 0 or pos.realized:
            results.append({
                "pnl": pos.resolution_pnl(winner),
                "layers": pos.layer_pnl(winner),
                "regime": rt.regime.label if rt.regime else "-",
                "combined_avg": pos.combined_avg,
                "fills": len(pos.fills),
                "settled_by": source,
            })
        hub.markets.pop(rt.market.condition_id, None)

    def settle_expired(now: float) -> None:
        """Fallback: the original run logs no 'resolved' event for windows it never
        traded, but THIS replay may have traded them — settle on last spot so their
        positions don't linger and strangle the risk caps."""
        for rt in list(hub.markets.values()):
            if rt.resolved or now < rt.market.end_ts + 5:
                continue
            spot = spots.get(rt.market.asset)
            if rt.market.strike is not None and spot and spot.price:
                winner = Side.YES if spot.price > rt.market.strike else Side.NO
                settle(rt, winner, "spot_fallback")
            else:
                rt.resolved = True
                sim.cancel_market(rt.market.condition_id)
                hub.markets.pop(rt.market.condition_id, None)

    for path in paths:
        f = path / "events.jsonl" if path.is_dir() else path
        if not f.exists():
            print(f"skip {f} (not found)", file=sys.stderr)
            continue
        with f.open() as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                clock["now"] = ev["ts"]
                sim.tick(ev["ts"])
                settle_expired(ev["ts"])
                t = ev["type"]
                if t == "market_discovered":
                    m = Market(condition_id=ev["condition_id"], slug=ev["slug"],
                               question=ev["question"], asset=ev["asset"],
                               duration_s=ev["duration_s"], start_ts=ev["start_ts"],
                               end_ts=ev["end_ts"],
                               token={Side.YES: ev["token_yes"], Side.NO: ev["token_no"]})
                    hub.markets[m.condition_id] = MarketRuntime(market=m)
                elif t == "strike":
                    rt = hub.markets.get(ev["market_id"])
                    if rt:
                        rt.market.strike = ev["strike"]
                elif t == "spot_bar":
                    spots[ev["asset"]].on_bar(ev)
                    engine.kick_asset(ev["asset"])
                elif t == "book_top":
                    rt = hub.markets.get(ev["market_id"])
                    if rt:
                        side = Side(ev["side"])
                        rt.books[side] = BookTop(
                            bid=ev["bid"], bid_size=ev.get("bid_size", 0),
                            ask=ev["ask"], ask_size=ev.get("ask_size", 0),
                            bid_depth_usdc=ev.get("bid_depth", 0),
                            ask_depth_usdc=ev.get("ask_depth", 0), ts=ev["ts"])
                        engine.kick_market(ev["market_id"])
                elif t == "trade_print":
                    sim.on_trade_print(ev["token"], ev["price"])
                elif t == "resolved":
                    rt = hub.markets.get(ev["market_id"])
                    if rt and not rt.resolved:
                        # tie rule: Up wins only strictly above the open
                        winner = (Side.YES if ev.get("close") is not None
                                  and ev["close"] > ev["strike"] else Side.NO)
                        settle(rt, winner, "recorded")
    # settle anything still open at the end of the corpus
    settle_expired(clock["now"] + 10_000)
    return report(results, sim)


def report(results: list[dict], sim: SimExecutor) -> dict:
    if not results:
        print("no traded windows in the replayed data")
        return {}
    pnls = [r["pnl"] for r in results]
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_regime[r["regime"]].append(r)
    cavgs = [r["combined_avg"] for r in results if r["combined_avg"]]
    total = sum(pnls)
    out = {
        "windows_traded": len(results),
        "total_pnl": round(total, 2),
        "expectancy_per_window": round(total / len(results), 4),
        "hit_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
        "max_drawdown": round(_max_drawdown(pnls), 2),
        "layer1_pnl": round(sum(r["layers"][1] for r in results), 2),
        "layer2_pnl": round(sum(r["layers"][2] for r in results), 2),
        "pnl_by_regime": {k: {"windows": len(v), "pnl": round(sum(x["pnl"] for x in v), 2),
                              "l1": round(sum(x["layers"][1] for x in v), 2),
                              "l2": round(sum(x["layers"][2] for x in v), 2)}
                          for k, v in by_regime.items()},
        "combined_avg": {"mean": round(statistics.mean(cavgs), 4) if cavgs else None,
                         "under_1_frac": round(sum(1 for c in cavgs if c < 1) / len(cavgs), 3) if cavgs else None},
        "fill_rate": round(sim.filled_orders / sim.submitted, 3) if sim.submitted else None,
        "orders_submitted": sim.submitted,
    }
    print(json.dumps(out, indent=2))
    if out["pnl_by_regime"].get("trending", {}).get("l1", 0) < -abs(out["pnl_by_regime"].get("choppy", {}).get("l1", 0)):
        print("\n⚠ GATE FAIL: layer-1 trending bleed exceeds its choppy profit (plan.md §8.2)")
    print(f"\nROI arithmetic: {out['expectancy_per_window']}/window × windows/day at this size "
          f"= decide consciously whether that justifies going live (plan.md §8.0)")
    return out


def _max_drawdown(pnls: list[float]) -> float:
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    run([Path(p) for p in sys.argv[1:]])
