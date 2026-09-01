"""Paper executor: queue-realistic simulated fills.

Fill rules (deliberately pessimistic):
- marketable on arrival (after latency): fill at the ask, capped at visible ask size;
- resting buy fills only when a trade PRINTS strictly below our limit (trades through),
  or the book crosses below it — a touch is not a fill;
- TTL expiry and fast-cancel on adverse spot moves, same as live.
Sells are taker-only (at the bid, capped at bid size).
"""
from __future__ import annotations

import asyncio
import logging
import time

from bot.models import Action, Fill, Order, OrderIntent, OrderStatus, Side
from bot.signal.fair_value import effective_cost

log = logging.getLogger("paper")


class PaperExecutor:
    def __init__(self, settings, hub, spot_states, recorder) -> None:
        self.s = settings
        self.hub = hub
        self.spots = spot_states
        self.recorder = recorder
        self.orders: dict[int, Order] = {}
        self._pending: list[tuple[float, OrderIntent]] = []  # (activate_ts, intent)

    # ---- interface ---------------------------------------------------------
    def submit(self, intent: OrderIntent) -> None:
        self._pending.append((time.time() + self.s.paper_latency_ms / 1000.0, intent))

    def cancel_market(self, market_id: str, why: str = "") -> int:
        n = 0
        for o in list(self.orders.values()):
            if o.intent.market_id == market_id and o.status == OrderStatus.RESTING:
                self._cancel(o, why or "cancel_market")
                n += 1
        return n

    def cancel_all(self, why: str = "") -> int:
        n = 0
        for o in list(self.orders.values()):
            if o.status == OrderStatus.RESTING:
                self._cancel(o, why or "cancel_all")
                n += 1
        self._pending.clear()
        return n

    def open_shares(self, market_id: str, side: Side) -> float:
        """BUY shares still working (queued for latency or resting) for market/side."""
        total = sum(i.shares for _, i in self._pending
                    if i.market_id == market_id and i.side is side and i.action is Action.BUY)
        total += sum(o.remaining for o in self.orders.values()
                     if o.status == OrderStatus.RESTING and o.intent.market_id == market_id
                     and o.intent.side is side and o.intent.action is Action.BUY)
        return total

    def on_trade_print(self, token_id: str, price: float) -> None:
        """A real trade printed on the book — resting buys below it fill through."""
        for o in list(self.orders.values()):
            if o.status != OrderStatus.RESTING or o.intent.token_id != token_id:
                continue
            if o.intent.action is Action.BUY and price < o.intent.price:
                self._fill(o, o.intent.price, o.remaining)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            now = time.time()
            self._activate(now)
            self._sweep(now)

    # ---- internals -------------------------------------------------------
    def _activate(self, now: float) -> None:
        due = [(t, i) for t, i in self._pending if t <= now]
        self._pending = [(t, i) for t, i in self._pending if t > now]
        for _, intent in due:
            rt = self.hub.markets.get(intent.market_id)
            if rt is None or rt.resolved or self.hub.halted:
                continue
            spot = self.spots.get(rt.market.asset)
            order = Order(intent=intent, spot_at_place=spot.price if spot else None)
            self.orders[intent.id] = order
            top = rt.books[intent.side]
            if intent.action is Action.SELL:
                if top.bid is not None:
                    shares = min(intent.shares, max(top.bid_size, 1.0))
                    self._fill(order, top.bid, shares, taker=True)
                else:
                    self._cancel(order, "no bid to sell into")
                continue
            # BUY: marketable on arrival?
            if top.ask is not None and top.ask <= intent.price:
                shares = min(intent.shares, max(top.ask_size, 1.0))
                self._fill(order, top.ask, shares, taker=True)
                if order.remaining > 0.5:
                    order.status = OrderStatus.RESTING
                else:
                    continue
            else:
                order.status = OrderStatus.RESTING
            self.recorder.log("order", {"id": intent.id, "market_id": intent.market_id,
                                        "side": intent.side.value, "action": intent.action.value,
                                        "price": intent.price, "shares": intent.shares,
                                        "signal": intent.signal.value, "status": order.status.value})

    def _sweep(self, now: float) -> None:
        for o in list(self.orders.values()):
            if o.status != OrderStatus.RESTING:
                continue
            i = o.intent
            if now - o.placed_ts > self.s.order_ttl_s:
                self._cancel(o, "ttl", expired=True)
                continue
            rt = self.hub.markets.get(i.market_id)
            if rt is None or rt.resolved:
                self._cancel(o, "market gone")
                continue
            # crossed book: best ask fell below our resting bid -> fill at our limit
            top = rt.books[i.side]
            if i.action is Action.BUY and top.ask is not None and top.ask < i.price:
                self._fill(o, i.price, o.remaining)
                continue
            # fast-cancel on adverse spot move
            spot = self.spots.get(rt.market.asset)
            if spot and spot.price and o.spot_at_place:
                move = (spot.price - o.spot_at_place) / o.spot_at_place
                adverse = -move if i.side is Side.YES else move
                if i.action is Action.BUY and adverse > self.s.fast_cancel_spot_move:
                    self._cancel(o, f"fast-cancel: spot moved {move:+.4%}")

    def _fill(self, order: Order, price: float, shares: float, taker: bool = True) -> None:
        if shares <= 0:
            return
        i = order.intent
        rt = self.hub.markets.get(i.market_id)
        fee_bps = rt.market.taker_fee_bps if (rt and taker) else (rt.market.maker_fee_bps if rt else 0.0)
        fee = (effective_cost(price, fee_bps) - price) * shares
        order.filled_shares += shares
        order.status = OrderStatus.FILLED if order.remaining < 0.5 else OrderStatus.PARTIAL
        fill = Fill(market_id=i.market_id, side=i.side, action=i.action, price=price,
                    shares=shares, fee=fee, signal=i.signal, order_id=i.id)
        rec = self.hub.on_fill(fill)
        self.recorder.log("fill", rec)
        log.info("FILL %s %s %.1f @ %.3f (%s)", i.action.value, i.side.value, shares, price, i.signal.value)

    def _cancel(self, order: Order, why: str, expired: bool = False) -> None:
        order.status = OrderStatus.EXPIRED if expired else OrderStatus.CANCELLED
        self.recorder.log("cancel", {"id": order.intent.id, "why": why,
                                     "market_id": order.intent.market_id})
