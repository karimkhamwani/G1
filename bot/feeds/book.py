"""Polymarket CLOB v2 market-channel websocket: order book + trade prints per market."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from bot.models import BookTop, Side

log = logging.getLogger("book")


class BookState:
    """Full L2 book for one token, maintained from snapshot + deltas."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ts: float = 0.0

    def snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = {float(x["price"]): float(x["size"]) for x in bids}
        self.asks = {float(x["price"]): float(x["size"]) for x in asks}
        self.ts = time.time()

    def change(self, price: float, side: str, size: float) -> None:
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.ts = time.time()

    def top(self) -> BookTop:
        bid = max(self.bids) if self.bids else None
        ask = min(self.asks) if self.asks else None
        return BookTop(
            bid=bid, bid_size=self.bids.get(bid, 0.0) if bid else 0.0,
            ask=ask, ask_size=self.asks.get(ask, 0.0) if ask else 0.0,
            bid_depth_usdc=sum(p * s for p, s in self.bids.items()),
            ask_depth_usdc=sum(p * s for p, s in self.asks.items()),
            ts=self.ts,
        )


class MarketBookFeed:
    """One websocket per market (its two tokens). Short-lived, like the markets."""

    def __init__(self, settings, hub, recorder, rt, on_update, on_trade) -> None:
        self.s = settings
        self.hub = hub
        self.recorder = recorder
        self.rt = rt                        # MarketRuntime
        self.on_update = on_update          # callback(market_id)
        self.on_trade = on_trade            # callback(token_id, price)
        self.books: dict[str, BookState] = {t: BookState() for t in rt.market.token.values()}
        self._last_recorded: dict[str, tuple] = {}

    async def run(self) -> None:
        m = self.rt.market
        url = f"{self.s.clob_ws}/ws/market"
        sub = json.dumps({"type": "market", "assets_ids": list(m.token.values())})
        backoff, fails = 1.0, 0
        while time.time() < m.end_ts + 30 and not self.rt.resolved:
            try:
                async with websockets.connect(url, ping_interval=10) as ws:
                    await ws.send(sub)
                    backoff, fails = 1.0, 0
                    async for raw in ws:
                        self.hub.feed_beat("book")
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        events = msg if isinstance(msg, list) else [msg]
                        for ev in events:
                            if isinstance(ev, dict):
                                self._handle(ev)
                        if self.rt.resolved or time.time() > m.end_ts + 30:
                            return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if time.time() >= m.end_ts:
                    return
                fails += 1
                if fails <= 2 or fails % 10 == 0:
                    log.warning("book feed %s error (%d): %r - retrying in %.0fs",
                                m.slug[:40], fails, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    def _handle(self, ev: dict) -> None:
        et = ev.get("event_type") or ev.get("type")
        token = ev.get("asset_id") or ev.get("assetId")
        if not token or token not in self.books:
            return
        book = self.books[token]
        if et == "book":
            book.snapshot(ev.get("bids") or ev.get("buys") or [],
                          ev.get("asks") or ev.get("sells") or [])
        elif et == "price_change":
            for ch in ev.get("changes", []) or [ev]:
                try:
                    book.change(float(ch["price"]), ch.get("side", "BUY"), float(ch["size"]))
                except (KeyError, ValueError, TypeError):
                    continue
        elif et == "last_trade_price":
            try:
                price = float(ev["price"])
            except (KeyError, ValueError, TypeError):
                return
            self.recorder.log("trade_print", {"market_id": self.rt.market.condition_id,
                                              "token": token, "price": price})
            self.on_trade(token, price)
            return
        else:
            return
        side = self.rt.market.side_of_token(token)
        if side is None:
            return
        top = book.top()
        self.rt.books[side] = top
        key = (top.bid, top.ask, round(top.bid_depth_usdc), round(top.ask_depth_usdc))
        if self._last_recorded.get(token) != key:
            self._last_recorded[token] = key
            self.recorder.log("book_top", {
                "market_id": self.rt.market.condition_id, "side": side.value,
                "bid": top.bid, "ask": top.ask, "bid_size": top.bid_size, "ask_size": top.ask_size,
                "bid_depth": round(top.bid_depth_usdc, 2), "ask_depth": round(top.ask_depth_usdc, 2),
            })
        self.on_update(self.rt.market.condition_id)
