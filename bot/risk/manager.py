"""Risk manager: hard caps the strategy cannot override, kill switch, feed fail-safes."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

from bot.models import Action, OrderIntent, SignalType

log = logging.getLogger("risk")


class RiskManager:
    def __init__(self, settings, hub, recorder) -> None:
        self.s = settings
        self.hub = hub
        self.recorder = recorder
        self.executor = None    # wired in main after construction
        self.check_staleness = True   # backtest replay disables (historical clocks)
        self._stale_cancelled = False

    # ---- per-intent validation (returns None if OK, else the veto reason) ---
    def validate(self, intent: OrderIntent, rt) -> str | None:
        hub, s = self.hub, self.s
        if hub.halted:
            return "halted"
        if intent.action is Action.SELL:
            return None  # reducing risk is always allowed
        if hub.paused:
            return "paused"
        pos = rt.position
        # caps must count working (resting/in-flight) BUY orders too, not just fills —
        # otherwise several concurrent orders each pass and collectively breach the cap
        pend_mkt_shares = pend_mkt_notional = pend_side_shares = pend_total_notional = 0.0
        open_buys = getattr(self.executor, "open_buy_orders", None)
        if open_buys:
            for market_id, side, shares, price in open_buys():
                pend_total_notional += price * shares
                if market_id == intent.market_id:
                    pend_mkt_shares += shares
                    pend_mkt_notional += price * shares
                    if side is intent.side:
                        pend_side_shares += shares
        if pos.total_shares + pend_mkt_shares + intent.shares > s.max_shares_per_market:
            return "max_shares_per_market"
        if pos.shares[intent.side] + pend_side_shares + intent.shares > s.max_shares_per_side:
            return "max_shares_per_side"
        if pos.cost_basis + pend_mkt_notional + intent.notional > s.max_per_market_usdc:
            return "max_per_market_usdc"
        if hub.total_exposure() + pend_total_notional + intent.notional > s.max_total_exposure_usdc:
            return "max_total_exposure"
        if pos.total_shares == 0 and hub.open_market_count() >= s.max_concurrent_markets:
            return "max_concurrent_markets"
        if intent.signal is SignalType.BASE_ENTRY:
            top = rt.books[intent.side]
            if top.bid_depth_usdc + top.ask_depth_usdc < s.min_book_depth_usdc:
                return "min_book_depth"
        if self.check_staleness and hub.feed_age("spot") > s.feed_stale_s:
            return "spot_stale"
        return None

    # ---- periodic checks -----------------------------------------------------
    async def run(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            self._daily_rollover()
            self._staleness_failsafe()
            self._kill_switch()

    def _daily_rollover(self) -> None:
        key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.hub.daily_key != key:
            self.hub.daily_key = key
            self.hub.daily_pnl = 0.0

    def _staleness_failsafe(self) -> None:
        """Feed lost while holding exposure -> cancel all resting orders immediately."""
        stale = (self.hub.feed_age("spot") > self.s.feed_stale_s * 3
                 or self.hub.feed_age("book") > self.s.feed_stale_s * 5)
        if stale and not self._stale_cancelled and self.hub.total_exposure() > 0:
            n = self.executor.cancel_all("feed stale fail-safe") if self.executor else 0
            self._stale_cancelled = True
            self.hub.note(f"FEED STALE: cancelled {n} resting orders (fail-safe)")
            self.recorder.log("failsafe", {"why": "feed_stale", "cancelled": n})
        elif not stale:
            self._stale_cancelled = False

    def _kill_switch(self) -> None:
        if self.hub.halted:
            return
        if self.hub.daily_pnl <= -self.s.max_daily_loss_usdc:
            self.halt(f"daily loss {self.hub.daily_pnl:.2f} breached -{self.s.max_daily_loss_usdc}")

    def halt(self, reason: str) -> None:
        self.hub.halted = True
        self.hub.halt_reason = reason
        n = self.executor.cancel_all("kill switch") if self.executor else 0
        self.hub.note(f"KILL SWITCH: {reason} (cancelled {n})")
        self.recorder.log("halt", {"reason": reason, "cancelled": n})
        log.error("KILL SWITCH: %s", reason)
        asyncio.get_running_loop().create_task(self.alert(f"🛑 bot halted: {reason}"))

    async def alert(self, text: str) -> None:
        if not self.s.telegram_bot_token or not self.s.telegram_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as http:
                await http.post(url, json={"chat_id": self.s.telegram_chat_id, "text": text},
                                timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:  # noqa: BLE001
            log.warning("telegram alert failed: %s", e)
