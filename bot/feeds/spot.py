"""Binance spot feed: last price, rolling volatility, momentum drift, window range."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from collections import deque

import websockets

log = logging.getLogger("spot")

SYMBOL = {"BTC": "btcusdt", "ETH": "ethusdt"}


class SpotState:
    def __init__(self, asset: str, vol_window_s: int, ewma_taus: list[float]) -> None:
        self.asset = asset
        self.price: float | None = None
        self.ts: float = 0.0
        self.vol_window_s = vol_window_s
        # 1-second bars: (ts, close)
        self.bars: deque[tuple[float, float]] = deque(maxlen=max(vol_window_s, 1200))
        self._bar_ts: int = 0
        self.ewma: dict[float, float] = {tau: 0.0 for tau in ewma_taus}  # log-return rate /s
        self._sigma_cache: tuple[float, float] = (0.0, 0.0)  # (computed_at, value)

    def on_trade(self, price: float, ts: float) -> bool:
        """Update state; returns True when a new 1s bar closed (worth recording)."""
        self.price, self.ts = price, ts
        sec = int(ts)
        new_bar = False
        if sec != self._bar_ts:
            if self.bars and self._bar_ts:
                prev_close = self.bars[-1][1]
                dt = max(1.0, sec - self._bar_ts)
                r = math.log(price / prev_close) / dt if prev_close > 0 else 0.0
                for tau in self.ewma:
                    a = 1.0 - math.exp(-dt / tau)
                    self.ewma[tau] = a * r + (1 - a) * self.ewma[tau]
            self.bars.append((float(sec), price))
            self._bar_ts = sec
            new_bar = True
        else:
            if self.bars:
                self.bars[-1] = (self.bars[-1][0], price)
        return new_bar

    @property
    def stale(self) -> float:
        return time.time() - self.ts if self.ts else float("inf")

    def sigma_per_sqrt_s(self) -> float:
        """Stdev of 1s log returns over the vol window (cached for 1s)."""
        now = time.time()
        if now - self._sigma_cache[0] < 1.0:
            return self._sigma_cache[1]
        cutoff = now - self.vol_window_s
        closes = [c for t, c in self.bars if t >= cutoff]
        if len(closes) < 30:
            self._sigma_cache = (now, 0.0)
            return 0.0
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        sig = statistics.pstdev(rets) if len(rets) > 2 else 0.0
        self._sigma_cache = (now, sig)
        return sig

    def drift_per_s(self) -> float:
        """Blend of the momentum EWMAs (log-return rate per second)."""
        if not self.ewma:
            return 0.0
        return sum(self.ewma.values()) / len(self.ewma)

    def prices_since(self, since_ts: float) -> list[float]:
        return [c for t, c in self.bars if t >= since_ts]


class SpotFeed:
    """One combined Binance websocket for all configured assets."""

    def __init__(self, settings, hub, recorder, on_tick) -> None:
        self.s = settings
        self.hub = hub
        self.recorder = recorder
        self.on_tick = on_tick  # callback(asset)
        self.states: dict[str, SpotState] = {
            a: SpotState(a, settings.vol_window_min * 60, settings.ewma_taus)
            for a in settings.asset_list if a in SYMBOL
        }

    async def run(self) -> None:
        streams = "/".join(f"{SYMBOL[a]}@trade" for a in self.states)
        url = f"{self.s.binance_ws}/stream?streams={streams}"
        sym_to_asset = {SYMBOL[a]: a for a in self.states}
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, max_queue=2048) as ws:
                    log.info("spot feed connected: %s", streams)
                    self.hub.note("spot feed connected")
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        if data.get("e") != "trade":
                            continue
                        sym = data["s"].lower()
                        asset = sym_to_asset.get(sym)
                        if not asset:
                            continue
                        price = float(data["p"])
                        ts = data["T"] / 1000.0
                        st = self.states[asset]
                        new_bar = st.on_trade(price, ts)
                        self.hub.feed_beat("spot")
                        if new_bar:
                            self.recorder.log("spot_bar", {
                                "asset": asset, "price": price, "bar_ts": int(ts),
                                "sigma": st.sigma_per_sqrt_s(), "drift": st.drift_per_s(),
                            })
                        self.on_tick(asset)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect on any transport error
                log.warning("spot feed error: %r — reconnecting in 2s", e)
                self.hub.note(f"spot feed lost ({e}); reconnecting")
                await asyncio.sleep(2)

    def snapshot(self) -> dict:
        return {a: {"price": st.price, "stale": round(st.stale, 1) if st.ts else None,
                    "drift": st.drift_per_s(), "sigma": st.sigma_per_sqrt_s()}
                for a, st in self.states.items()}
