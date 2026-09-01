"""Live executor: Polymarket CLOB v2 via the official py-clob-client (L2 auth).

USE ONLY AFTER THE VALIDATION GATES IN plan.md PASS. Requires:
    pip install '.[live]'
    POLYGON_WALLET_PRIVATE_KEY (+ optionally pre-derived API creds) in .env

The client is synchronous, so calls run in a thread executor. Fills arrive via the
CLOB v2 user-channel websocket; REST reconciliation runs on connect.

NOTE: redemption of resolved positions is an on-chain ConditionalTokens call and is
NOT automated here yet - winnings must be redeemed via the Polymarket UI (logged as a
reminder at each resolution).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from bot.models import Action, Fill, Order, OrderIntent, OrderStatus, SignalType

log = logging.getLogger("live")


class LiveExecutor:
    def __init__(self, settings, hub, spot_states, recorder) -> None:
        self.s = settings
        self.hub = hub
        self.spots = spot_states
        self.recorder = recorder
        self.orders: dict[str, Order] = {}   # exchange order id -> Order
        self.client = None
        self.creds = None
        self._init_client()

    def _init_client(self) -> None:
        try:
            from py_clob_client.client import ClobClient
        except ImportError as e:
            raise RuntimeError("MODE=live requires py-clob-client: pip install '.[live]'") from e
        self.client = ClobClient(
            self.s.clob_host, key=self.s.polygon_wallet_private_key, chain_id=self.s.chain_id,
        )
        if self.s.polymarket_api_key:
            from py_clob_client.clob_types import ApiCreds
            self.creds = ApiCreds(api_key=self.s.polymarket_api_key,
                                  api_secret=self.s.polymarket_api_secret,
                                  api_passphrase=self.s.polymarket_api_passphrase)
        else:
            self.creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(self.creds)
        log.info("CLOB v2 client ready (L2 auth)")

    # ---- interface (same as PaperExecutor) --------------------------------
    def submit(self, intent: OrderIntent) -> None:
        asyncio.get_running_loop().create_task(self._place(intent))

    def cancel_market(self, market_id: str, why: str = "") -> int:
        n = 0
        for oid, o in list(self.orders.items()):
            if o.intent.market_id == market_id and o.status == OrderStatus.RESTING:
                asyncio.get_running_loop().create_task(self._cancel(oid, why))
                n += 1
        return n

    def cancel_all(self, why: str = "") -> int:
        try:
            asyncio.get_running_loop().create_task(self._cancel_all(why))
        except RuntimeError:
            pass
        return len(self.orders)

    def open_shares(self, market_id: str, side) -> float:
        """BUY shares still resting at the exchange for this market/side."""
        return sum(o.remaining for o in self.orders.values()
                   if o.status == OrderStatus.RESTING and o.intent.market_id == market_id
                   and o.intent.side is side and o.intent.action is Action.BUY)

    def on_trade_print(self, token_id: str, price: float) -> None:
        pass  # real fills come from the user channel

    async def run(self) -> None:
        await asyncio.gather(self._user_channel(), self._sweep_loop())

    # ---- order placement ------------------------------------------------
    async def _place(self, intent: OrderIntent) -> None:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        loop = asyncio.get_running_loop()
        args = OrderArgs(
            token_id=intent.token_id,
            price=round(intent.price, 3),
            size=round(intent.shares, 2),
            side=BUY if intent.action is Action.BUY else SELL,
        )
        try:
            signed = await loop.run_in_executor(None, self.client.create_order, args)
            resp = await loop.run_in_executor(
                None, lambda: self.client.post_order(signed, OrderType.GTC))
            oid = (resp or {}).get("orderID") or (resp or {}).get("orderId")
            if not oid:
                log.warning("order rejected: %s", resp)
                self.recorder.log("order", {"id": intent.id, "status": "REJECTED", "resp": str(resp)[:200]})
                return
            spot = self.spots.get(self.hub.markets[intent.market_id].market.asset)
            order = Order(intent=intent, status=OrderStatus.RESTING,
                          spot_at_place=spot.price if spot else None, exchange_id=oid)
            self.orders[oid] = order
            self.recorder.log("order", {"id": intent.id, "exchange_id": oid,
                                        "market_id": intent.market_id, "side": intent.side.value,
                                        "action": intent.action.value, "price": intent.price,
                                        "shares": intent.shares, "signal": intent.signal.value,
                                        "status": "RESTING"})
        except Exception as e:  # noqa: BLE001
            log.error("order placement failed: %s", e)
            self.recorder.log("order_error", {"id": intent.id, "error": str(e)[:200]})

    async def _cancel(self, exchange_id: str, why: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.client.cancel, exchange_id)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel %s failed: %s", exchange_id[:12], e)
        order = self.orders.get(exchange_id)
        if order:
            order.status = OrderStatus.CANCELLED
        self.recorder.log("cancel", {"exchange_id": exchange_id, "why": why})

    async def _cancel_all(self, why: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.client.cancel_all)
            self.recorder.log("cancel_all", {"why": why})
        except Exception as e:  # noqa: BLE001
            log.error("cancel_all failed: %s", e)

    # ---- user channel: fills -----------------------------------------------
    async def _user_channel(self) -> None:
        url = f"{self.s.clob_ws}/ws/user"
        auth = {"apiKey": self.creds.api_key, "secret": self.creds.api_secret,
                "passphrase": self.creds.api_passphrase}
        while True:
            try:
                async with websockets.connect(url, ping_interval=10) as ws:
                    await ws.send(json.dumps({"type": "user", "auth": auth, "markets": []}))
                    self.hub.note("CLOB user channel connected")
                    async for raw in ws:
                        self.hub.feed_beat("user")
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        for ev in (msg if isinstance(msg, list) else [msg]):
                            if isinstance(ev, dict) and ev.get("event_type") == "trade":
                                self._on_trade_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("user channel error: %s - reconnecting", e)
                await asyncio.sleep(2)

    def _on_trade_event(self, ev: dict) -> None:
        try:
            oid = ev.get("taker_order_id") or ev.get("order_id") or ""
            order = self.orders.get(oid)
            price = float(ev["price"])
            size = float(ev.get("size") or ev.get("matched_amount") or 0)
        except (KeyError, ValueError, TypeError):
            return
        if order is None or size <= 0:
            return
        i = order.intent
        rt = self.hub.markets.get(i.market_id)
        fee_bps = rt.market.taker_fee_bps if rt else 0.0
        fee = (fee_bps / 10_000.0) * min(price, 1 - price) * size
        order.filled_shares += size
        order.status = OrderStatus.FILLED if order.remaining < 0.5 else OrderStatus.PARTIAL
        fill = Fill(market_id=i.market_id, side=i.side, action=i.action, price=price,
                    shares=size, fee=fee, signal=i.signal, order_id=i.id)
        rec = self.hub.on_fill(fill)
        self.recorder.log("fill", rec)
        log.info("LIVE FILL %s %s %.1f @ %.3f", i.action.value, i.side.value, size, price)

    # ---- ttl + fast-cancel sweep ------------------------------------------
    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            now = time.time()
            for oid, o in list(self.orders.items()):
                if o.status != OrderStatus.RESTING:
                    continue
                i = o.intent
                if now - o.placed_ts > self.s.order_ttl_s:
                    await self._cancel(oid, "ttl")
                    continue
                rt = self.hub.markets.get(i.market_id)
                if rt is None:
                    continue
                spot = self.spots.get(rt.market.asset)
                if spot and spot.price and o.spot_at_place and i.action is Action.BUY:
                    move = (spot.price - o.spot_at_place) / o.spot_at_place
                    adverse = -move if i.side.value == "YES" else move
                    if adverse > self.s.fast_cancel_spot_move:
                        await self._cancel(oid, f"fast-cancel {move:+.4%}")
