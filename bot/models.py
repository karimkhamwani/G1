from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    YES = "YES"
    NO = "NO"

    @property
    def other(self) -> "Side":
        return Side.NO if self is Side.YES else Side.YES


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalType(str, Enum):
    BASE_ENTRY = "BASE_ENTRY"
    SCALE_ADD = "SCALE_ADD"
    SKEW = "SKEW"
    TAKE_PROFIT = "TAKE_PROFIT"


LAYER_OF = {
    SignalType.BASE_ENTRY: 1,
    SignalType.SCALE_ADD: 1,
    SignalType.SKEW: 2,
    SignalType.TAKE_PROFIT: 2,
}


@dataclass
class Market:
    condition_id: str
    slug: str
    question: str
    asset: str            # BTC / ETH
    duration_s: int
    start_ts: float
    end_ts: float
    token: dict[Side, str]   # Side -> CLOB token id
    strike: float | None = None      # spot at window open (captured live)

    def side_of_token(self, token_id: str) -> Side | None:
        for s, t in self.token.items():
            if t == token_id:
                return s
        return None


@dataclass
class BookTop:
    bid: float | None = None
    bid_size: float = 0.0
    ask: float | None = None
    ask_size: float = 0.0
    bid_depth_usdc: float = 0.0
    ask_depth_usdc: float = 0.0
    ts: float = 0.0

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def imbalance(self) -> float:
        """(bid depth − ask depth) / total, in [-1, 1]. Positive = buy pressure."""
        tot = self.bid_depth_usdc + self.ask_depth_usdc
        if tot <= 0:
            return 0.0
        return (self.bid_depth_usdc - self.ask_depth_usdc) / tot


_intent_seq = itertools.count(1)


@dataclass
class OrderIntent:
    market_id: str
    token_id: str
    side: Side
    action: Action
    price: float
    shares: float
    signal: SignalType
    reason: str = ""
    id: int = field(default_factory=lambda: next(_intent_seq))

    @property
    def notional(self) -> float:
        return self.price * self.shares


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    RESTING = "RESTING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    intent: OrderIntent
    placed_ts: float = field(default_factory=time.time)
    status: OrderStatus = OrderStatus.PENDING
    filled_shares: float = 0.0
    spot_at_place: float | None = None
    exchange_id: str | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.intent.shares - self.filled_shares)


@dataclass
class Fill:
    market_id: str
    side: Side
    action: Action
    price: float
    shares: float
    fee: float
    signal: SignalType
    ts: float = field(default_factory=time.time)
    order_id: int = 0

    def as_dict(self) -> dict:
        return {
            "market_id": self.market_id, "side": self.side.value, "action": self.action.value,
            "price": round(self.price, 4), "shares": round(self.shares, 2),
            "fee": round(self.fee, 6), "signal": self.signal.value, "ts": self.ts,
        }
