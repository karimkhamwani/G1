"""Market lifecycle: strike capture at window open, resolution at window close.

Settlement is OFFICIAL-FIRST: the window's real result comes from Polymarket
(Gamma `outcomePrices` once `umaResolutionStatus=resolved`) because the oracle is a
Chainlink 60s TWAP — the last spot-feed tick can disagree in the final seconds (a
last-second dip barely moves a 60s TWAP), which used to book winning trades as
losses. The spot proxy is only a timeout fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from bot.models import Side

log = logging.getLogger("lifecycle")

STRIKE_GRACE_S = 5        # how late we may still capture the open price
RESOLVE_DELAY_S = 3       # start settling a moment after close
OFFICIAL_POLL_S = 10      # how often to ask Gamma for the official result
OFFICIAL_TIMEOUT_S = 300  # fall back to the spot proxy after this long
UA = {"User-Agent": "Mozilla/5.0 (momentum-bot)"}


def official_winner(row: dict) -> Side | None:
    """Map a Gamma market row to the official winner, or None if not resolved yet."""
    if not row or not row.get("closed"):
        return None
    if str(row.get("umaResolutionStatus") or "").lower() != "resolved":
        return None
    try:
        outcomes = row.get("outcomes")
        prices = row.get("outcomePrices")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else (outcomes or [])
        prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
        prices = [float(x) for x in prices]
    except (ValueError, TypeError):
        return None
    if len(outcomes) != 2 or len(prices) != 2 or max(prices) < 0.99:
        return None
    up_idx = 0
    for i, o in enumerate(outcomes[:2]):
        if str(o).strip().lower() in ("up", "yes"):
            up_idx = i
    return Side.YES if prices[up_idx] >= 0.99 else Side.NO


class Lifecycle:
    def __init__(self, settings, hub, spot_states, executor, recorder, risk) -> None:
        self.s = settings
        self.hub = hub
        self.spots = spot_states
        self.executor = executor
        self.recorder = recorder
        self.risk = risk
        self._http: aiohttp.ClientSession | None = None

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                now = time.time()
                for rt in list(self.hub.markets.values()):
                    self._capture_strike(rt, now)
                    await self._resolve(rt, now)
                self._prune(now)
        finally:
            if self._http:
                await self._http.close()

    async def _fetch_official(self, m) -> Side | None:
        if self._http is None:
            self._http = aiohttp.ClientSession(headers=UA)
        try:
            async with self._http.get(f"{self.s.gamma_host}/markets",
                                      params={"slug": m.slug, "closed": "true"},
                                      timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                rows = await resp.json()
        except Exception as e:  # noqa: BLE001
            log.debug("official fetch %s failed: %s", m.slug, e)
            return None
        return official_winner(rows[0]) if rows else None

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

    async def _resolve(self, rt, now: float) -> None:
        m = rt.market
        if rt.resolved or now < m.end_ts + RESOLVE_DELAY_S:
            return
        if not rt.awaiting_official:
            # window just closed: cancel working orders, snapshot the close spot for
            # reference, and start polling for the OFFICIAL result
            self.executor.cancel_market(m.condition_id, "window closed")
            if m.strike is None or rt.position.total_shares == 0:
                rt.resolved = True
                if rt.position.fills:
                    # traded but fully exited before the close (stop-loss / take-profit):
                    # the realized P&L must still be booked and the cycle recorded —
                    # with zero shares left the outcome doesn't change the number
                    spot = self.spots.get(m.asset)
                    close = spot.price if spot and spot.stale < 10 else None
                    self._settle(rt, now, None, close, "closed_out")
                self.hub.markets.pop(m.condition_id, None)
                return
            spot = self.spots.get(m.asset)
            rt.close_spot = spot.price if spot and spot.stale < 10 else None
            rt.awaiting_official = True
            rt.next_official_poll = now
        if now < rt.next_official_poll:
            return
        rt.next_official_poll = now + OFFICIAL_POLL_S
        winner = await self._fetch_official(m)
        if winner is not None:
            rt.resolved = True
            self._settle(rt, now, winner, rt.close_spot, "official")
            return
        if now > m.end_ts + OFFICIAL_TIMEOUT_S:
            # fallback: last-tick spot proxy (tie -> Down). Known to disagree with the
            # Chainlink 60s-TWAP oracle near the close — flagged in the record.
            rt.resolved = True
            if rt.close_spot is not None and m.strike is not None:
                winner = Side.YES if rt.close_spot > m.strike else Side.NO
                self._settle(rt, now, winner, rt.close_spot, "spot_proxy_fallback")
            else:
                self.hub.note(f"{m.slug[:40]}: no official result and no close price - "
                              "booking worst case, check manually")
                winner = min(Side, key=lambda s: rt.position.shares[s])
                self._settle(rt, now, winner, None, "no_price_worst_case")

    def _settle(self, rt, now: float, winner: Side | None, close, settlement: str) -> None:
        """winner=None means the position was fully exited before resolution — with
        zero remaining shares the P&L is purely the realized amount and does not
        depend on the outcome (any Side gives the same numbers)."""
        m = rt.market
        w = winner if winner is not None else Side.NO
        pnl = rt.position.resolution_pnl(w)
        layers = rt.position.layer_pnl(w)
        rt.winner, rt.resolution_pnl, rt.layer_pnl = winner, pnl, layers
        self.hub.book_pnl(pnl)
        record = {
            "ts": round(now, 3),
            "market_id": m.condition_id, "slug": m.slug, "question": m.question,
            "asset": m.asset, "duration_s": m.duration_s, "mode": self.s.mode,
            "winner": winner.value if winner else "EXITED", "close": close, "strike": m.strike,
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
        outcome = winner.value if winner else "closed out early"
        self.hub.note(f"resolved {m.asset} {m.duration_s // 60}m -> {outcome} "
                      f"pnl {pnl:+.2f} (L1 {layers[1]:+.2f} / L2 {layers[2]:+.2f})")
        if self.s.mode == "live":
            self.hub.note("live mode: redeem winnings in the Polymarket UI (auto-redeem TODO)")

    def _prune(self, now: float) -> None:
        for cid, rt in list(self.hub.markets.items()):
            # never prune a market still awaiting its official result
            if rt.resolved and now > rt.market.end_ts + 60:
                self.hub.markets.pop(cid, None)
