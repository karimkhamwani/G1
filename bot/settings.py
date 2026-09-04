"""All configuration comes from .env (see .env.example). Parsed and validated at startup."""
from __future__ import annotations

from functools import cached_property

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DURATION_S = {"5m": 300, "15m": 900, "1h": 3600}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # market selection
    market_slug: str = "bitcoin-up-or-down"
    assets: str = "BTC,ETH"
    durations: str = "5m,15m"

    # run mode
    mode: str = "paper"

    # signal
    vol_window_min: int = 30
    momentum_ewma_s: str = "5,30"
    final_blackout_s: int = 20

    # scaling (layer 1)
    base_shares: float = 20
    entry_window_s: int = 30
    repair_max_slip: float = 0.03       # abandon a base leg repair rather than chase this far
    add_trigger_drop: float = 0.05
    add_step_shares: float = 10
    add_jitter_pct: float = 0.25
    chop_score_min: float = 2.0
    min_net_move_frac: float = 0.0008   # regime: net move below this = "no character yet"
    min_window_age_s: int = 20          # no ladder adds before the window is this old
    pair_add_max: float = 0.98          # add a matched pair when ask_yes + ask_no <= this
    max_adds_per_side_5m: int = 3
    step_decay_5m: float = 0.7
    max_adds_per_side_15m: int = 6
    step_decay_15m: float = 0.85
    max_shares_per_side: float = 60

    # skew (layer 2)
    # strategy: "directional" (one-sided conviction bets) or "paired" (base + pair ladder + skew)
    strategy: str = "directional"
    dir_conf_min: float = 0.75          # model confidence required on a side
    dir_min_entry_price: float = 0.70   # ... AND that side's ask must be at or ABOVE this
                                        # (the book agrees with the model)
    dir_require_edge: bool = False      # optional: also require ask < model fair value (off:
                                        # both thresholds met = enter, whichever crossed first)
    dir_market_budget_usdc: float = 10  # hard cap on total spend per market
    dir_step_shares: float = 5          # shares per bet
    dir_conf_step: float = 0.02         # confidence OR book ask must rise this much for the next bet
    dir_exit_below: float = 0.45        # sell ALL if the held side's ask AND confidence <= this
    buy_order_type: str = "FOK"         # FOK (all-or-nothing) or FAK (partial fills allowed)

    skew_enabled: bool = True           # False = layer 2 never opens a position
    skew_window_s: int = 60             # skew fires only in the last N seconds of the window
    skew_min_fair: float = 0.75         # model conviction floor for the favored side
    skew_threshold: float = 0.05
    skew_step_shares: float = 10
    max_skew_shares: float = 40
    book_imbalance_min: float = 0.2

    # risk
    max_shares_per_market: float = 120
    max_per_market_usdc: float = 25
    max_concurrent_markets: int = 4
    max_total_exposure_usdc: float = 150
    max_daily_loss_usdc: float = 40
    feed_stale_s: float = 2

    # dashboard
    dashboard_port: int = 8080
    dashboard_host: str = "127.0.0.1"

    # execution
    min_order_shares: float = 5    # Polymarket rejects orders below this size
    order_cross_ticks: int = 2     # BUY limit = ask + this many ticks (slippage cap; keeps
                                   # the order marketable despite placement latency)
    order_ttl_s: float = 10
    min_book_depth_usdc: float = 200
    take_profit_levels: str = "0.90,0.97"
    fast_cancel_spot_move: float = 0.0008
    paper_latency_ms: int = 2000   # measured live placement latency (signal -> exchange)

    # endpoints
    clob_host: str = "https://clob.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    binance_ws: str = "wss://stream.binance.com:9443"
    chain_id: int = 137

    # recording
    data_dir: str = "data"

    # credentials
    polygon_wallet_private_key: str = ""
    polymarket_funder_address: str = ""   # proxy wallet holding the funds (Polymarket UI account)
    polymarket_signature_type: int = 1    # 1 = email/Magic login proxy, 2 = browser-wallet proxy
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @field_validator("mode")
    @classmethod
    def _mode_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("paper", "live"):
            raise ValueError("MODE must be 'paper' or 'live'")
        return v

    @field_validator("strategy")
    @classmethod
    def _strategy_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("directional", "paired"):
            raise ValueError("STRATEGY must be 'directional' or 'paired'")
        return v

    @field_validator("buy_order_type")
    @classmethod
    def _order_type_ok(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("FOK", "FAK"):
            raise ValueError("BUY_ORDER_TYPE must be 'FOK' or 'FAK'")
        return v

    # ---- parsed views -------------------------------------------------
    @cached_property
    def slug_list(self) -> list[str]:
        return [s.strip().lower() for s in self.market_slug.split(",") if s.strip()]

    @cached_property
    def asset_list(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    @cached_property
    def duration_list_s(self) -> list[int]:
        out = []
        for d in self.durations.split(","):
            d = d.strip().lower()
            if d not in DURATION_S:
                raise ValueError(f"unsupported duration {d!r} (supported: {list(DURATION_S)})")
            out.append(DURATION_S[d])
        return out

    @cached_property
    def ewma_taus(self) -> list[float]:
        return [float(x) for x in self.momentum_ewma_s.split(",") if x.strip()]

    @cached_property
    def tp_levels(self) -> list[float]:
        return sorted(float(x) for x in self.take_profit_levels.split(",") if x.strip())

    def ladder(self, duration_s: int) -> tuple[int, float]:
        """(max_adds_per_side, step_decay) for a market duration."""
        if duration_s <= 300:
            return self.max_adds_per_side_5m, self.step_decay_5m
        return self.max_adds_per_side_15m, self.step_decay_15m

    def validate_live(self) -> None:
        if self.mode == "live" and not self.polygon_wallet_private_key:
            raise ValueError("MODE=live requires POLYGON_WALLET_PRIVATE_KEY in .env")
