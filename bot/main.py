"""Entrypoint: python -m bot.main   (MODE comes from .env: paper | live)"""
from __future__ import annotations

import asyncio
import logging
import signal as os_signal

from bot.discovery.gamma import Discovery
from bot.feeds.book import MarketBookFeed
from bot.feeds.spot import SpotFeed
from bot.lifecycle import Lifecycle
from bot.monitor.server import build_app, serve
from bot.record.recorder import Recorder
from bot.risk.manager import RiskManager
from bot.settings import Settings
from bot.signal.engine import SignalEngine
from bot.state import Hub, MarketRuntime

log = logging.getLogger("main")


class App:
    def __init__(self) -> None:
        self.settings = Settings()
        self.settings.validate_live()
        self.hub = Hub()
        self.hub.mode = self.settings.mode
        self.recorder = Recorder(self.settings.data_dir)
        self.hub.load_trade_log(self.recorder.trades_path, mode=self.settings.mode)
        self.risk = RiskManager(self.settings, self.hub, self.recorder)

        if self.settings.mode == "live":
            from bot.execution.live import LiveExecutor
            executor_cls = LiveExecutor
        else:
            from bot.execution.paper import PaperExecutor
            executor_cls = PaperExecutor

        self.spot_feed = SpotFeed(self.settings, self.hub, self.recorder, self._on_spot_tick)
        self.executor = executor_cls(self.settings, self.hub, self.spot_feed.states, self.recorder)
        self.risk.executor = self.executor
        self.engine = SignalEngine(self.settings, self.hub, self.spot_feed.states,
                                   self.risk, self.executor, self.recorder)
        self.discovery = Discovery(self.settings, self.hub, self.recorder, self._on_new_market)
        self.lifecycle = Lifecycle(self.settings, self.hub, self.spot_feed.states,
                                   self.executor, self.recorder, self.risk)
        self._book_tasks: dict[str, asyncio.Task] = {}

    # ---- wiring callbacks -------------------------------------------------
    def _on_spot_tick(self, asset: str) -> None:
        self.engine.kick_asset(asset)

    def _on_new_market(self, market) -> None:
        rt = MarketRuntime(market=market)
        self.hub.markets[market.condition_id] = rt
        feed = MarketBookFeed(self.settings, self.hub, self.recorder, rt,
                              on_update=self.engine.kick_market,
                              on_trade=self.executor.on_trade_print)
        self._book_tasks[market.condition_id] = asyncio.get_running_loop().create_task(feed.run())

    # ---- run ---------------------------------------------------------------
    async def run(self) -> None:
        s = self.settings
        log.info("starting in %s mode | assets=%s durations=%s",
                 s.mode.upper(), s.asset_list, s.durations)
        self.hub.note(f"bot started in {s.mode.upper()} mode")
        tasks = [
            asyncio.create_task(self.spot_feed.run(), name="spot"),
            asyncio.create_task(self.discovery.run(), name="discovery"),
            asyncio.create_task(self.executor.run(), name="executor"),
            asyncio.create_task(self.risk.run(), name="risk"),
            asyncio.create_task(self.lifecycle.run(), name="lifecycle"),
            asyncio.create_task(self.recorder.flush_loop(), name="recorder"),
        ]
        if s.dashboard_port > 0:
            app = build_app(self.hub, self.spot_feed, self.executor, self.risk,
                            trades_path=self.recorder.trades_path)
            tasks.append(asyncio.create_task(
                serve(app, s.dashboard_host, s.dashboard_port), name="dashboard"))
            log.info("dashboard: http://%s:%s", s.dashboard_host, s.dashboard_port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass  # Windows: Ctrl+C arrives as KeyboardInterrupt via main() instead
        try:
            await stop.wait()
        finally:
            log.info("shutting down: cancelling orders and flushing recorder")
            # await the exchange-side cancel — a fire-and-forget cancel would race
            # loop teardown and could leave live GTC orders resting after exit
            try:
                await self.executor.aclose()
            except Exception as e:  # noqa: BLE001
                log.error("shutdown cancel failed: %s — check the exchange for orders", e)
            for t in [*tasks, *self._book_tasks.values()]:
                t.cancel()
            await asyncio.gather(*tasks, *self._book_tasks.values(), return_exceptions=True)
            self.recorder.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        asyncio.run(App().run())
    except KeyboardInterrupt:
        log.info("stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
