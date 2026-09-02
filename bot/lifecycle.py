"""Market lifecycle: strike capture at window open, resolution at window close."""
from __future__ import annotations

import asyncio
import logging
import time

from bot.models import Side

log = logging.getLogger("lifecycle")

STRIKE_GRACE_S = 5        # how late we may still capture the open price
RESOLVE_DELAY_S = 3       # settle a moment after close so the last spot bar lands
SETTLE_MAX_STALE_S = 5    # close price must be at most this old to settle
SETTLE_TIMEOUT_S = 90     # give up waiting for a fresh price after this long


class Lifecycle:
    def __init__(self, settings, hub, spot_states, executor, recorder, risk) -> None:
        self.s = settings
        self.hub = hub
        self.spots = spot_states
        self.executor = executor
        self.recorder = recorder
        self.risk = risk

    async def run(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = time.time()
            for rt in list(self.hub.markets.values()):
                self._capture_strike(rt, now)
                self._resolve(rt, now)
            self._prune(now)

    def _capture_strike(self, rt, now: float) -> None:
        m = rt.market
        if m.strike is not None or rt.skipped or now < m.start_ts:
            return
        spot = self.spots.get(m.asset)
        if now - m.start_ts <= STRIKE_GRACE_S and spot and spot.price and spot.stale < 2:
            m.strike = spot.price
            self.recorder.log("strike", {"market_id": m.condition_id, "strike": m.strike})
            self.hub.note(f"{m.asset} {m.duration_s // 60}m window open @ {m.strike:.2f}")
        elif now - m.start_ts > STRIKE_GRACE_S:
            rt.skipped = "joined late - no reliable open price"
            self.recorder.log("skip", {"market_id": m.condition_id, "why": rt.skipped})

    def _resolve(self, rt, now: float) -> None:
        m = rt.market
        if rt.resolved or now < m.end_ts + RESOLVE_DELAY_S:
            return
        self.executor.cancel_market(m.condition_id, "window closed")
        if m.strike is None or rt.position.total_shares == 0:
            rt.resolved = True
            self.hub.markets.pop(m.condition_id, None)
            return
        spot = self.spots.get(m.asset)
        settlement = "spot_proxy"
        if spot is None or spot.price is None or spot.stale > SETTLE_MAX_STALE_S:
            # a stale price can pick the WRONG winner — wait for a fresh tick,
            # and only give up after a timeout with a conservative worst case
            if now < m.end_ts + SETTLE_TIMEOUT_S:
                return   # not resolved yet — retry next tick
            self.hub.note(f"{m.slug[:40]}: no fresh close price after "
                          f"{SETTLE_TIMEOUT_S}s - booking worst case, check manually")
            rt.resolved = True
            winner = min(Side, key=lambda s: rt.position.shares[s])  # worst case: fewer-shares side wins
            close = None
            settlement = "no_price_worst_case"
            self._settle(rt, now, winner, close, settlement)
            return
        close = spot.price
        rt.resolved = True
        # NOTE: settles on Binance spot as an oracle PROXY. The real oracle can disagree;
        # oracle-mismatch measurement is a backtest report item.
        # Tie rule: these markets resolve UP only if close is strictly ABOVE the open —
        # a tie is Down/NO (plan.md: "closes above the window's open price").
        winner = Side.YES if close > m.strike else Side.NO
        self._settle(rt, now, winner, close, settlement)

    def _settle(self, rt, now: float, winner: Side, close, settlement: str) -> None:
        m = rt.market
        pnl = rt.position.resolution_pnl(winner)
        layers = rt.position.layer_pnl(winner)
        rt.winner, rt.resolution_pnl, rt.layer_pnl = winner, pnl, layers
        self.hub.book_pnl(pnl)
        record = {
            "ts": round(now, 3),
            "market_id": m.condition_id, "slug": m.slug, "question": m.question,
            "asset": m.asset, "duration_s": m.duration_s, "mode": self.s.mode,
            "winner": winner.value, "close": close, "strike": m.strike,
            "pnl": round(pnl, 4),
            "l1": round(layers[1], 4), "l2": round(layers[2], 4),
            "combined_avg": round(rt.position.combined_avg, 4) if rt.position.combined_avg else None,
            "matched": round(rt.position.matched, 2),
            "net_side": rt.position.skew_side.value if rt.position.skew_side else None,
            "net_shares": round(rt.position.skew_shares, 2),
            "skew_side": rt.position.skew_l2_side.value if rt.position.skew_l2_side else None,
            "skew_bought": round(rt.position.skew_bought, 2),
            "fees_paid": round(rt.position.fees_paid, 4),
            "regime": rt.regime.label if rt.regime else "-",
            "fills": len(rt.position.fills),
            "fill_rate": (lambda o, b: round(b / o, 3) if o > 0 else None)(
                rt.position.ordered[Side.YES] + rt.position.ordered[Side.NO],
                rt.position.bought[Side.YES] + rt.position.bought[Side.NO]),
            "settlement": settlement,
        }
        self.hub.history.append(record)
        self.recorder.log("resolved", {k: v for k, v in record.items() if k != "ts"})
        self.hub.note(f"resolved {m.asset} {m.duration_s // 60}m -> {winner.value} "
                      f"pnl {pnl:+.2f} (L1 {layers[1]:+.2f} / L2 {layers[2]:+.2f})")
        if self.s.mode == "live":
            self.hub.note("live mode: redeem winnings in the Polymarket UI (auto-redeem TODO)")

    def _prune(self, now: float) -> None:
        for cid, rt in list(self.hub.markets.items()):
            if rt.resolved and now > rt.market.end_ts + 60:
                self.hub.markets.pop(cid, None)
