"""Append-only JSONL recorder, partitioned by UTC day.

Every feed message, signal, order event, fill, and halt goes through here;
the day's file is the backtest corpus. JSONL (not a columnar format) so a
crash mid-write never corrupts previous records.

Trading events are ALSO appended to a single rolling `data/trades.jsonl`
(no feed noise) — that file is the analysis log, and the dashboard rebuilds
its fills tape / history / equity curve from it on startup.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

TRADE_EVENTS = {"signal", "veto", "order", "order_error", "fill", "cancel", "cancel_all",
                "resolved", "strike", "skip", "halt", "failsafe", "market_discovered"}


class Recorder:
    def __init__(self, data_dir: str) -> None:
        self.root = Path(data_dir)
        self._file: IO | None = None
        self._day: str = ""
        self._pending = 0
        self._trades: IO | None = None

    @property
    def trades_path(self) -> Path:
        return self.root / "trades.jsonl"

    def _trades_file(self) -> IO:
        if self._trades is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._trades = self.trades_path.open("a", encoding="utf-8")
        return self._trades

    def _ensure_file(self) -> IO:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._file is None or day != self._day:
            if self._file:
                self._file.close()
            path = self.root / day
            path.mkdir(parents=True, exist_ok=True)
            self._file = (path / "events.jsonl").open("a", encoding="utf-8")
            self._day = day
        return self._file

    def log(self, event_type: str, payload: dict) -> None:
        rec = {"ts": round(time.time(), 4), "type": event_type, **payload}
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        f = self._ensure_file()
        f.write(line)
        self._pending += 1
        if self._pending >= 200:
            f.flush()
            self._pending = 0
        if event_type in TRADE_EVENTS:
            tf = self._trades_file()
            tf.write(line)
            tf.flush()  # low volume; keep the analysis log crash-current

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self._file and self._pending:
                self._file.flush()
                self._pending = 0

    def close(self) -> None:
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
        if self._trades:
            self._trades.flush()
            self._trades.close()
            self._trades = None
