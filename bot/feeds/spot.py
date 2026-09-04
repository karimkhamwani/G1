"""Binance spot feed: last price, rolling volatility, momentum drift, window range."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import ssl
import statistics
import time

import websockets

log = logging.getLogger("spot")

SYMBOL = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt",
          "BNB": "bnbusdt", "XRP": "xrpusdt", "DOGE": "dogeusdt"}

MAX_BAR_STEP_S = 5.0        # a step longer than this spans a feed gap: not a valid return
MIN_VOL_SAMPLES = 60        # usable returns required before sigma is trusted
MIN_CREDIBLE_SIGMA = 5e-6   # below this the tape has stalled; treat sigma as unknown


def _ssl_context() -> ssl.SSLContext:
    """Default verification, minus Python 3.13's VERIFY_X509_STRICT (some exchange
    CA chains fail the strict check; certs are still fully verified)."""
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


class SpotState:
    def __init__(self, asset: str, vol_window_s: int, ewma_taus: list[float]) -> None:
        self.asset = asset
        self.price: float | None = None
        self.ts: float = 0.0
        self.vol_window_s = vol_window_s
        # 1-second bars: (ts, close)
        self.bars: list[tuple[float, float]] = []
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
            if len(self.bars) > max(self.vol_window_s, 1200) + 600:
                self.bars = self.bars[-max(self.vol_window_s, 1200):]
            self._bar_ts = sec
            new_bar = True
        elif self.bars:
            self.bars[-1] = (self.bars[-1][0], price)
        return new_bar

    @property
    def stale(self) -> float:
        return time.time() - self.ts if self.ts else float("inf")

    def sigma_per_sqrt_s(self) -> float:
        """Per-sqrt-second stdev of log returns over the vol window (cached for 1s).

        Returns 0.0 when the estimate is not trustworthy — the caller MUST treat that
        as "no volatility estimate" and refuse to trade, never as "zero volatility".
        A gappy feed used to collapse this number (returns were taken between
        consecutive stored bars regardless of the seconds between them, and thin
        stretches contribute runs of identical prices), which saturated the fair-value
        CDF and turned sub-basis-point noise into false conviction.
        """
        now = time.time()
        if now - self._sigma_cache[0] < 1.0:
            return self._sigma_cache[1]
        cutoff = now - self.vol_window_s
        win = [(t, c) for t, c in self.bars if t >= cutoff]
        rets = []
        for (t0, c0), (t1, c1) in zip(win, win[1:]):
            dt = t1 - t0
            # skip steps that span a feed gap: they are not 1s observations
            if 0 < dt <= MAX_BAR_STEP_S and c0 > 0 and c1 > 0:
                rets.append(math.log(c1 / c0) / math.sqrt(dt))
        if len(rets) < MIN_VOL_SAMPLES:
            self._sigma_cache = (now, 0.0)
            return 0.0
        sig = statistics.pstdev(rets)
        if sig < MIN_CREDIBLE_SIGMA:
            # a near-zero reading means a stalled tape, not a calm market
            sig = 0.0
        self._sigma_cache = (now, sig)
        return sig

    def vol_ready(self) -> bool:
        """True when there is a usable volatility estimate."""
        return self.sigma_per_sqrt_s() > 0.0

    def drift_per_s(self) -> float:
        """Blend of the momentum EWMAs (log-return rate per second)."""
        if not self.ewma:
            return 0.0
        return sum(self.ewma.values()) / len(self.ewma)

    def prices_since(self, since_ts: float) -> list[float]:
        return [p for t, p in self.bars if t >= since_ts]


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
        unmapped = [a for a in settings.asset_list if a not in SYMBOL]
        if unmapped:
            log.error("no Binance symbol mapping for %s - these assets will NOT be "
                      "priced or traded (add them to SYMBOL in feeds/spot.py)", unmapped)
            hub.note(f"ignoring unmapped assets: {unmapped}")

    async def run(self) -> None:
        streams = "/".join(f"{SYMBOL[a]}@trade" for a in self.states)
        url = f"{self.s.binance_ws}/stream?streams={streams}"
        sym_to_asset = {SYMBOL[a]: a for a in self.states}
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, max_queue=2048,
                                              ssl=_ssl_context()) as ws:
                    log.info("spot feed connected: %s", streams)
                    self.hub.note("spot feed connected")
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        if data.get("e") != "trade":
                            continue
                        asset = sym_to_asset.get(data["s"].lower())
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
            except Exception as e:  # noqa: BLE001 - reconnect on any transport error
                log.warning("spot feed error: %r - reconnecting in 2s", e)
                self.hub.note(f"spot feed lost ({e}); reconnecting")
                await asyncio.sleep(2)

    def snapshot(self) -> dict:
        return {a: {"price": st.price, "stale": round(st.stale, 1) if st.ts else None,
                    "drift": st.drift_per_s(), "sigma": st.sigma_per_sqrt_s()}
                for a, st in self.states.items()}
