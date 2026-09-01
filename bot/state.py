"""Central shared state: active market runtimes, spot states, session stats.

The dashboard reads snapshots from here; feeds and the executor write into it.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from bot.models import BookTop, Fill, Market, Side
from bot.execution.tracker import Position
from bot.signal.regime import Regime


@dataclass
class MarketRuntime:
    market: Market
    books: dict[Side, BookTop] = field(default_factory=lambda: {Side.YES: BookTop(), Side.NO: BookTop()})
    position: Position = field(default_factory=Position)
    regime: Regime | None = None
    fair_yes: float | None = None
    confluence_dir: int = 0            # +1 YES, -1 NO, 0 none
    skipped: str | None = None         # reason this market is not traded (e.g. joined late)
    resolved: bool = False
    winner: Side | None = None
    resolution_pnl: float | None = None
    layer_pnl: dict[int, float] | None = None
    last_eval_ts: float = 0.0

    @property
    def mid(self) -> dict[Side, float | None]:
        return {s: self.books[s].mid for s in Side}

    def active(self, now: float) -> bool:
        m = self.market
        return (not self.resolved and not self.skipped
                and m.strike is not None and m.start_ts <= now < m.end_ts)


@dataclass
class ResolvedCycle:
    market: Market
    winner: Side
    pnl: float
    layer_pnl: dict[int, float]
    combined_avg: float | None
    matched: float
    skew_side: str | None
    skew_shares: float
    regime: str
    fills: int
    resolved_ts: float


class Hub:
    def __init__(self) -> None:
        self.markets: dict[str, MarketRuntime] = {}
        self.history: list[ResolvedCycle] = []
        self.fills: deque[Fill] = deque(maxlen=500)
        self.equity_curve: deque[tuple[float, float]] = deque(maxlen=2000)
        self.session_pnl: float = 0.0
        self.daily_pnl: float = 0.0
        self.daily_key: str = ""
        self.paused: bool = False            # no new entries
        self.halted: bool = False            # kill switch fired
        self.halt_reason: str = ""
        self.mode: str = "paper"
        self.started_ts: float = time.time()
        self.feed_ts: dict[str, float] = {}  # feed name -> last message ts
        self.notes: deque[str] = deque(maxlen=100)

    # ---- accounting ----------------------------------------------------
    def total_exposure(self) -> float:
        """Cost basis of all unresolved positions (open + awaiting redemption)."""
        return sum(rt.position.cost_basis for rt in self.markets.values() if not rt.resolved)

    def open_market_count(self) -> int:
        return sum(1 for rt in self.markets.values()
                   if not rt.resolved and rt.position.total_shares > 0)

    def on_fill(self, fill: Fill) -> None:
        rt = self.markets.get(fill.market_id)
        if rt:
            rt.position.apply_fill(fill)
        self.fills.appendleft(fill)

    def book_pnl(self, amount: float) -> None:
        self.session_pnl += amount
        self.daily_pnl += amount
        self.equity_curve.append((time.time(), self.session_pnl))

    def note(self, msg: str) -> None:
        self.notes.appendleft(f"{time.strftime('%H:%M:%S')} {msg}")

    def feed_beat(self, name: str) -> None:
        self.feed_ts[name] = time.time()

    def feed_age(self, name: str) -> float:
        ts = self.feed_ts.get(name)
        return time.time() - ts if ts else float("inf")

    # ---- dashboard snapshot ---------------------------------------------
    def snapshot(self, spots: dict | None = None) -> dict:
        now = time.time()
        active_cards = []
        for rt in self.markets.values():
            if rt.resolved:
                continue
            p, m = rt.position, rt.market
            active_cards.append({
                "id": m.condition_id[:10],
                "question": m.question,
                "asset": m.asset,
                "duration": m.duration_s,
                "t_rem": max(0, round(m.end_ts - now)),
                "strike": m.strike,
                "fair_yes": round(rt.fair_yes, 3) if rt.fair_yes is not None else None,
                "regime": rt.regime.label if rt.regime else "-",
                "chop": round(rt.regime.chop_score, 2) if rt.regime else None,
                "confluence": rt.confluence_dir,
                "skipped": rt.skipped,
                "books": {s.value: {
                    "bid": rt.books[s].bid, "ask": rt.books[s].ask,
                } for s in Side},
                "pos": {
                    "yes_shares": round(p.shares[Side.YES], 2),
                    "no_shares": round(p.shares[Side.NO], 2),
                    "yes_avg": round(p.avg(Side.YES), 4) if p.avg(Side.YES) else None,
                    "no_avg": round(p.avg(Side.NO), 4) if p.avg(Side.NO) else None,
                    "combined_avg": round(p.combined_avg, 4) if p.combined_avg else None,
                    "matched": round(p.matched, 2),
                    "skew_side": p.skew_side.value if p.skew_side else None,
                    "skew_shares": round(p.skew_shares, 2),
                    "adds_yes": p.adds_used[Side.YES],
                    "adds_no": p.adds_used[Side.NO],
                    "unrealized": (lambda u: round(u, 2) if u is not None else None)(p.unrealized(rt.mid)),
                },
            })
        return {
            "ts": now,
            "mode": self.mode,
            "paused": self.paused,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "session_pnl": round(self.session_pnl, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "exposure": round(self.total_exposure(), 2),
            "feeds": {name: round(self.feed_age(name), 1) if self.feed_age(name) != float("inf") else None
                      for name in self.feed_ts},
            "spots": spots or {},
            "markets": active_cards,
            "fills": [f.as_dict() for f in list(self.fills)[:60]],
            "equity": [[round(t, 1), round(v, 2)] for t, v in self.equity_curve],
            "history": [{
                "question": h.market.question, "winner": h.winner.value,
                "pnl": round(h.pnl, 2),
                "l1": round(h.layer_pnl.get(1, 0), 2), "l2": round(h.layer_pnl.get(2, 0), 2),
                "combined_avg": round(h.combined_avg, 4) if h.combined_avg else None,
                "regime": h.regime, "fills": h.fills, "ts": h.resolved_ts,
            } for h in self.history[-100:]],
            "notes": list(self.notes)[:30],
        }
