"""Live executor: Polymarket CLOB v2 via the official py-clob-client-v2 (L2 auth).

USE ONLY AFTER THE VALIDATION GATES IN plan.md PASS. Requires:
    pip install '.[live]'      (installs py-clob-client-v2)
    POLYGON_WALLET_PRIVATE_KEY (+ optionally pre-derived API creds) in .env

The client is synchronous, so calls run in a thread executor. Fills arrive via the
CLOB v2 user-channel websocket (both taker and maker fills).

Startup reconciliation: any orders left at the exchange by a previous run are
CANCELLED at startup (clean slate). Pre-existing POSITIONS are not adopted — the bot
warns and ignores them; redeem or manage those in the Polymarket UI.

NOTE: redemption of resolved positions is an on-chain ConditionalTokens call and is
NOT automated here yet - winnings must be redeemed via the Polymarket UI (logged as a
reminder at each resolution).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict

import websockets

from bot.models import Action, Fill, Order, OrderIntent, OrderStatus

log = logging.getLogger("live")


class LiveExecutor:
    def __init__(self, settings, hub, spot_states, recorder) -> None:
        self.s = settings
        self.hub = hub
        self.spots = spot_states
        self.recorder = recorder
        self.orders: dict[str, Order] = {}        # exchange order id -> Order
        self._inflight: dict[int, OrderIntent] = {}   # intent.id -> intent, during placement
        self._bg: set[asyncio.Task] = set()       # tracked background cancel/place tasks
        self.client = None
        self.creds = None
        self._consec_errors = 0
        self._cooldown_until = 0.0
        # The user channel re-sends each trade as its status advances
        # (MATCHED -> MINED -> CONFIRMED), so a fill must be booked exactly once.
        self._seen_trades: OrderedDict[str, None] = OrderedDict()
        self._init_client()

    def _init_client(self) -> None:
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
        except ImportError as e:
            raise RuntimeError("MODE=live requires py-clob-client-v2: pip install '.[live]'") from e
        kwargs: dict = {}
        if self.s.polymarket_funder_address:
            # Polymarket UI accounts hold funds in a proxy wallet: orders are signed by
            # the exported key but funded/settled by the proxy ("funder") address.
            # signature_type 1 = email/Magic login, 2 = browser-wallet (Gnosis safe).
            kwargs["signature_type"] = self.s.polymarket_signature_type
            kwargs["funder"] = self.s.polymarket_funder_address
            log.info("proxy-wallet mode: funder=%s signature_type=%s",
                     self.s.polymarket_funder_address, self.s.polymarket_signature_type)
        else:
            log.info("EOA mode: trading directly from the key's own address")
        self.client = ClobClient(
            host=self.s.clob_host, chain_id=self.s.chain_id,
            key=self.s.polygon_wallet_private_key, **kwargs,
        )
        if self.s.polymarket_api_key:
            self.creds = ApiCreds(api_key=self.s.polymarket_api_key,
                                  api_secret=self.s.polymarket_api_secret,
                                  api_passphrase=self.s.polymarket_api_passphrase)
        else:
            self.creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(self.creds)
        log.info("CLOB client ready (py-clob-client-v2, L2 auth)")

    def _track(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    # ---- interface (same as PaperExecutor) --------------------------------
    def submit(self, intent: OrderIntent) -> None:
        if time.time() < self._cooldown_until:
            # dropped, not deferred — tell the hub so one-shot state (TP levels) re-arms
            self.hub.order_closed(intent, 0.0)
            return
        # register BEFORE the async placement so the engine's pending gate sees it
        self._inflight[intent.id] = intent
        self._track(self._place(intent))

    def cancel_market(self, market_id: str, why: str = "") -> int:
        n = 0
        for oid, o in list(self.orders.items()):
            if o.intent.market_id == market_id and o.status == OrderStatus.RESTING:
                self._track(self._cancel(oid, why))
                n += 1
        return n

    def cancel_all(self, why: str = "") -> int:
        n = sum(1 for o in self.orders.values() if o.status == OrderStatus.RESTING)
        try:
            self._track(self._cancel_all(why))
        except RuntimeError:
            pass
        return n

    async def aclose(self) -> None:
        """Awaited on shutdown: make sure the exchange-side cancel actually lands."""
        await self._cancel_all("shutdown")
        pending = [t for t in self._bg if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def open_shares(self, market_id: str, side) -> float:
        """BUY shares working for this market/side: in-flight placements + resting."""
        total = sum(i.shares for i in self._inflight.values()
                    if i.market_id == market_id and i.side is side and i.action is Action.BUY)
        total += sum(o.remaining for o in self.orders.values()
                     if o.status == OrderStatus.RESTING and o.intent.market_id == market_id
                     and o.intent.side is side and o.intent.action is Action.BUY)
        return total

    def open_buy_orders(self) -> list[tuple[str, object, float, float]]:
        """(market_id, side, shares, price) for every working BUY — used by risk caps."""
        out = [(i.market_id, i.side, i.shares, i.price) for i in self._inflight.values()
               if i.action is Action.BUY]
        out += [(o.intent.market_id, o.intent.side, o.remaining, o.intent.price)
                for o in self.orders.values()
                if o.status == OrderStatus.RESTING and o.intent.action is Action.BUY]
        return out

    def on_trade_print(self, token_id: str, price: float) -> None:
        pass  # real fills come from the user channel

    async def run(self) -> None:
        await self._reconcile_startup()
        await asyncio.gather(self._user_channel(), self._sweep_loop())

    # ---- startup reconciliation -----------------------------------------------
    async def _reconcile_startup(self) -> None:
        """Clean slate: cancel any orders a previous run left at the exchange.

        Orphaned resting orders from a crashed/killed run would otherwise fill
        invisibly (their ids are unknown to this process). Positions cannot be
        safely adopted into per-market accounting — warn instead.
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.client.cancel_all)
            self.recorder.log("cancel_all", {"why": "startup reconciliation (clean slate)"})
            self.hub.note("startup: cancelled any orders left by a previous run")
        except Exception as e:  # noqa: BLE001
            log.error("startup cancel_all failed: %s — orphaned orders may exist!", e)
            self.recorder.log("cancel_all_failed", {"why": "startup reconciliation",
                                                    "error": str(e)[:200]})
            self.hub.note(f"WARNING: startup order cleanup failed ({e}) — check the "
                          "Polymarket UI for orphaned orders")
        self.hub.note("note: positions from previous runs are NOT tracked — "
                      "redeem/manage them in the Polymarket UI")

    # ---- order placement ------------------------------------------------
    async def _place(self, intent: OrderIntent) -> None:
        from py_clob_client_v2 import OrderArgs, OrderType
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions
        loop = asyncio.get_running_loop()
        try:
            # conditions may have changed while this task waited to run
            rt = self.hub.markets.get(intent.market_id)
            if self.hub.halted or rt is None or rt.resolved:
                self.hub.order_closed(intent, 0.0)
                return
            # BUYs go out as FAK (fill-and-kill) with a small cross buffer: the limit is
            # ask + N ticks so the order is still marketable after ~2s of placement
            # latency (a limit fills at the MAKER's price, so the buffer is a slippage
            # cap, not a cost), and whatever can't fill immediately dies instead of
            # resting stale. SELLs (take-profit) rest as GTC at their level.
            is_buy = intent.action is Action.BUY
            tick = rt.market.tick_size or 0.01
            price = intent.price
            if is_buy:
                price = min(1.0 - tick, price + self.s.order_cross_ticks * tick)
            price = round(round(price / tick) * tick, 4)   # snap to the market's grid
            order_type = OrderType.FAK if is_buy else OrderType.GTC
            # whole shares: FAK buys require the USDC maker amount (price x size) to
            # have <= 2 decimals — integer size x on-grid price guarantees it
            size = float(int(round(intent.shares))) if is_buy else round(intent.shares, 2)
            args = OrderArgs(
                token_id=intent.token_id,
                price=price,
                size=size,
                side=intent.action.value,        # v2 takes "BUY"/"SELL" strings
            )
            # tick size + neg_risk come from discovery, so create_order signs locally
            # with ZERO metadata round-trips — the POST is the only network hop left
            options = PartialCreateOrderOptions(tick_size=f"{tick:g}",
                                                neg_risk=rt.market.neg_risk)
            def _sign_and_post():
                # both steps in ONE background thread: signing is ~1ms local CPU and
                # posting strictly depends on it, so a second thread handoff between
                # them would only add latency
                signed = self.client.create_order(args, options)
                return self.client.post_order(signed, order_type=order_type)

            try:
                resp = await loop.run_in_executor(None, _sign_and_post)
                oid = (resp or {}).get("orderID") or (resp or {}).get("orderId")
                if not oid:
                    log.warning("order rejected: %s", resp)
                    self.recorder.log("order", {"id": intent.id, "status": "REJECTED",
                                                "resp": str(resp)[:200]})
                    self.hub.order_closed(intent, 0.0)
                    return
                spot = self.spots.get(rt.market.asset)
                order = Order(intent=intent, status=OrderStatus.RESTING,
                              spot_at_place=spot.price if spot else None, exchange_id=oid)
                self.orders[oid] = order
                self._consec_errors = 0
                latency_ms = round((time.time() - intent.created_ts) * 1000)
                self.recorder.log("order", {"id": intent.id, "exchange_id": oid,
                                            "market_id": intent.market_id, "side": intent.side.value,
                                            "action": intent.action.value, "price": price,
                                            "shares": intent.shares, "signal": intent.signal.value,
                                            "order_type": order_type, "status": "PLACED",
                                            "latency_ms": latency_ms})
                log.info("placed %s %s %.1f @ %.3f (%s, %dms signal->exchange)",
                         intent.action.value, intent.side.value, intent.shares, price,
                         order_type, latency_ms)
            except Exception as e:  # noqa: BLE001
                if "no orders found to match" in str(e).lower():
                    # NOT an error: this is FAK's normal no-liquidity outcome — the
                    # order was killed unfilled, exactly as designed. Never let it
                    # feed the error counter/cooldown (that silently drops the next
                    # orders for up to 30s).
                    self.recorder.log("order", {"id": intent.id, "market_id": intent.market_id,
                                                "side": intent.side.value,
                                                "signal": intent.signal.value,
                                                "status": "FAK_NO_MATCH"})
                    log.info("FAK no match: %s %s %.0f sh (no liquidity at limit)",
                             intent.action.value, intent.side.value, intent.shares)
                    self.hub.order_closed(intent, 0.0)
                    return
                self._consec_errors += 1
                self._cooldown_until = time.time() + min(2 * self._consec_errors, 30)
                if "not enough balance" in str(e).lower():
                    self.hub.note("insufficient balance/allowance: free USDC is likely "
                                  "locked in UNREDEEMED winnings - redeem resolved "
                                  "positions in the Polymarket UI")
                log.error("order placement failed (%d consecutive): %s", self._consec_errors, e)
                self.recorder.log("order_error", {"id": intent.id, "error": str(e)[:200],
                                                  "consecutive": self._consec_errors})
                self.hub.order_closed(intent, 0.0)
                if self._consec_errors >= 10 and not self.hub.paused:
                    self.hub.paused = True
                    self.hub.note(f"AUTO-PAUSED: {self._consec_errors} consecutive order errors "
                                  f"({str(e)[:80]}) - fix the cause, then resume from the dashboard")
        finally:
            self._inflight.pop(intent.id, None)

    async def _cancel(self, exchange_id: str, why: str) -> None:
        from py_clob_client_v2.clob_types import OrderPayload
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self.client.cancel_order(OrderPayload(orderID=exchange_id)))
        except Exception as e:  # noqa: BLE001
            log.warning("cancel %s failed: %s", exchange_id[:12], e)
        order = self.orders.get(exchange_id)
        if order and order.status == OrderStatus.RESTING:
            order.status = OrderStatus.CANCELLED
            self.hub.order_closed(order.intent, order.filled_shares)
        self.recorder.log("cancel", {"exchange_id": exchange_id, "why": why})

    async def _cancel_all(self, why: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.client.cancel_all)
            self.recorder.log("cancel_all", {"why": why})
        except Exception as e:  # noqa: BLE001
            log.error("cancel_all failed: %s", e)
        for order in self.orders.values():
            if order.status == OrderStatus.RESTING:
                order.status = OrderStatus.CANCELLED
                self.hub.order_closed(order.intent, order.filled_shares)

    # ---- user channel: fills (taker AND maker) --------------------------------
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

    SEEN_TRADES_MAX = 5000

    def _trade_key(self, ev: dict, oid: str) -> str | None:
        """Stable identity for one (trade, our order) pair across status re-sends."""
        tid = ev.get("id") or ev.get("trade_id") or ev.get("tradeID")
        if tid:
            return f"{tid}:{oid}"
        # No trade id in this payload: fall back to the match time, which the
        # re-sends preserve (our own arrival time would not).
        ts = ev.get("match_time") or ev.get("matchtime") or ev.get("timestamp")
        if ts:
            return f"{oid}:{ts}:{ev.get('price')}:{ev.get('size')}"
        return None

    def _claim_trade(self, key: str | None, ev: dict) -> bool:
        """True the first time a trade is seen; False for every re-send."""
        if key is None:
            # Cannot dedupe — book it, but record the payload so the schema can be fixed.
            log.warning("trade event with no usable id — cannot dedupe: %s", str(ev)[:200])
            self.recorder.log("trade_no_id", {"event": str(ev)[:400]})
            return True
        if key in self._seen_trades:
            self.recorder.log("trade_duplicate", {"key": key, "status": ev.get("status")})
            return False
        self._seen_trades[key] = None
        while len(self._seen_trades) > self.SEEN_TRADES_MAX:
            self._seen_trades.popitem(last=False)
        return True

    def _on_trade_event(self, ev: dict) -> None:
        """Book fills whether our order was the taker or a maker in the trade.

        Each trade is booked once: the channel repeats it as its status advances,
        and a failed trade never settles at all.
        """
        if str(ev.get("status", "")).upper() == "FAILED":
            self.recorder.log("trade_failed", {"id": str(ev.get("id"))[:64]})
            return
        # taker side: our id in taker_order_id/order_id
        oid = ev.get("taker_order_id") or ev.get("order_id") or ""
        if oid in self.orders:
            try:
                price = float(ev["price"])
                size = float(ev.get("size") or ev.get("matched_amount") or 0)
            except (KeyError, ValueError, TypeError):
                return
            if not self._claim_trade(self._trade_key(ev, oid), ev):
                return
            self._book_fill(oid, price, size)
            return
        # maker side: our id inside the maker orders array
        for mo in ev.get("maker_orders") or []:
            if not isinstance(mo, dict):
                continue
            moid = mo.get("order_id") or mo.get("orderID") or ""
            if moid not in self.orders:
                continue
            try:
                price = float(mo.get("price") or ev.get("price"))
                size = float(mo.get("matched_amount") or mo.get("size") or 0)
            except (ValueError, TypeError):
                continue
            if not self._claim_trade(self._trade_key(ev, moid), ev):
                continue
            self._book_fill(moid, price, size)

    def _book_fill(self, exchange_id: str, price: float, size: float) -> None:
        order = self.orders.get(exchange_id)
        if order is None or size <= 0:
            return
        i = order.intent
        fee = 0.0   # fees are not modeled; Polymarket settles them on-chain
        order.filled_shares += size
        order.status = OrderStatus.FILLED if order.remaining < 0.5 else OrderStatus.PARTIAL
        fill = Fill(market_id=i.market_id, side=i.side, action=i.action, price=price,
                    shares=size, fee=fee, signal=i.signal, order_id=i.id)
        rec = self.hub.on_fill(fill)
        self.recorder.log("fill", rec)
        log.info("LIVE FILL %s %s %.1f @ %.3f", i.action.value, i.side.value, size, price)

    # ---- order lifecycle sweep ------------------------------------------
    FAK_REAP_S = 4.0   # a FAK is dead at the exchange instantly; its fills arrive via
                       # the user channel within moments — after this long, finalize it

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            now = time.time()
            for oid, o in list(self.orders.items()):
                if o.status != OrderStatus.RESTING:
                    if o.status in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                                    OrderStatus.EXPIRED) and now - o.placed_ts > 120:
                        self.orders.pop(oid, None)   # prune terminal orders
                    continue
                i = o.intent
                if i.action is Action.BUY:
                    # FAK: already dead at the exchange — no cancel needed, just
                    # finalize once its fills have had time to stream in
                    if now - o.placed_ts > self.FAK_REAP_S:
                        o.status = (OrderStatus.FILLED if o.filled_shares > 0
                                    else OrderStatus.CANCELLED)
                        self.hub.order_closed(i, o.filled_shares)
                        if o.filled_shares <= 0:
                            self.recorder.log("cancel", {"exchange_id": oid,
                                                         "why": "fak no fill"})
                    continue
                # GTC sells (take-profit): TTL as before
                if now - o.placed_ts > self.s.order_ttl_s:
                    await self._cancel(oid, "ttl")
