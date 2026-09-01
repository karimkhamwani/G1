"""Signal engine: BaseEntry, ScaleAdd (model + regime gated), Skew (confluence), TakeProfit.

Pure evaluation over shared state — no I/O — so the same code runs live, in paper
mode, and in backtest replay.
"""
from __future__ import annotations

import logging
import random
import time

from bot.models import Action, OrderIntent, Side, SignalType
from bot.signal.fair_value import effective_cost, fair_yes
from bot.signal.regime import classify

log = logging.getLogger("signal")

EVAL_THROTTLE_S = 0.2
MAX_SPREAD = 0.10          # don't trade a book wider than this
MIN_FAIR_CONVICTION = 0.02 # |fair-0.5| below this = model has no direction


class SignalEngine:
    def __init__(self, settings, hub, spot_states, risk, executor, recorder,
                 clock=time.time, rng: random.Random | None = None) -> None:
        self.s = settings
        self.hub = hub
        self.spots = spot_states           # asset -> SpotState
        self.risk = risk
        self.executor = executor
        self.recorder = recorder
        self.clock = clock
        self.rng = rng or random.Random()

    # ---- entry points ----------------------------------------------------
    def kick_market(self, market_id: str) -> None:
        rt = self.hub.markets.get(market_id)
        if rt:
            self.evaluate(rt)

    def kick_asset(self, asset: str) -> None:
        for rt in self.hub.markets.values():
            if rt.market.asset == asset and not rt.resolved:
                self.evaluate(rt)

    # ---- evaluation --------------------------------------------------------
    def evaluate(self, rt) -> list[OrderIntent]:
        now = self.clock()
        if now - rt.last_eval_ts < EVAL_THROTTLE_S:
            return []
        rt.last_eval_ts = now

        m, pos = rt.market, rt.position
        spot = self.spots.get(m.asset)
        if not rt.active(now) or spot is None or spot.price is None:
            return []

        t_rem = m.end_ts - now
        sigma, drift = spot.sigma_per_sqrt_s(), spot.drift_per_s()
        fair = fair_yes(spot.price, m.strike, sigma, drift, t_rem)
        rt.fair_yes = fair
        fair_of = {Side.YES: fair, Side.NO: 1.0 - fair}

        regime = classify(spot.prices_since(m.start_ts), m.strike, self.s.chop_score_min)
        rt.regime = regime

        intents: list[OrderIntent] = []
        intents += self._base_entry(rt, now)
        intents += self._scale_adds(rt, fair_of, regime)
        intents += self._skew(rt, fair_of, t_rem)
        intents += self._take_profit(rt)

        submitted = []
        for intent in intents:
            verdict = self.risk.validate(intent, rt)
            if verdict is None:
                self.recorder.log("signal", {
                    "market_id": m.condition_id, "signal": intent.signal.value,
                    "side": intent.side.value, "action": intent.action.value,
                    "price": intent.price, "shares": round(intent.shares, 2),
                    "fair_yes": round(fair, 4), "regime": regime.label,
                    "chop": round(regime.chop_score, 2), "reason": intent.reason,
                })
                if intent.action is Action.BUY:
                    rt.position.ordered[intent.side] += intent.shares
                self.executor.submit(intent)
                submitted.append(intent)
            else:
                self.recorder.log("veto", {"market_id": m.condition_id,
                                           "signal": intent.signal.value, "why": verdict})
        return submitted

    # ---- layer 1: base entry ------------------------------------------------
    def _base_entry(self, rt, now: float) -> list[OrderIntent]:
        m, pos = rt.market, rt.position
        if pos.base_placed or now - m.start_ts > self.s.entry_window_s:
            return []
        intents = []
        for side in Side:
            top = rt.books[side]
            if top.ask is None or top.spread is None or top.spread > MAX_SPREAD:
                return []   # need both books sane before committing either leg
            if top.ask_depth_usdc + top.bid_depth_usdc < self.s.min_book_depth_usdc:
                return []
            intents.append(OrderIntent(
                market_id=m.condition_id, token_id=m.token[side], side=side,
                action=Action.BUY, price=top.ask, shares=self.s.base_shares,
                signal=SignalType.BASE_ENTRY, reason="base two-sided entry",
            ))
        pos.base_placed = True
        return intents

    # ---- layer 1: ladder adds (model gate + regime gate) ---------------------
    def _scale_adds(self, rt, fair_of: dict, regime) -> list[OrderIntent]:
        m, pos = rt.market, rt.position
        if not pos.base_placed or regime.trending:
            return []
        max_adds, decay = self.s.ladder(m.duration_s)
        intents = []
        for side in Side:
            top, avg = rt.books[side], pos.avg(side)
            if top.ask is None or avg is None:
                continue
            if pos.adds_used[side] >= max_adds:
                continue
            # trigger: ask meaningfully below our own average
            if top.ask > avg - self.s.add_trigger_drop:
                continue
            # model gate: also cheap vs fair value, net of taker fee
            if effective_cost(top.ask, m.taker_fee_bps) > fair_of[side] - self.s.add_margin:
                continue
            step = self.s.add_step_shares * (decay ** pos.adds_used[side])
            step *= 1.0 + self.rng.uniform(-self.s.add_jitter_pct, self.s.add_jitter_pct)
            shares = max(1.0, round(step, 1))
            if pos.shares[side] + shares > self.s.max_shares_per_side:
                continue
            price = round(top.ask * (1.0 + self.rng.uniform(0, 0.01)), 3)  # tiny price jitter
            intents.append(OrderIntent(
                market_id=m.condition_id, token_id=m.token[side], side=side,
                action=Action.BUY, price=min(price, 0.99), shares=shares,
                signal=SignalType.SCALE_ADD,
                reason=f"avg {avg:.3f} -> ask {top.ask:.3f}, fair {fair_of[side]:.3f}, "
                       f"chop {regime.chop_score:.1f}",
            ))
        return intents

    # ---- layer 2: momentum skew ------------------------------------------------
    def _skew(self, rt, fair_of: dict, t_rem: float) -> list[OrderIntent]:
        m, pos = rt.market, rt.position
        if t_rem < self.s.final_blackout_s or pos.skew_bought >= self.s.max_skew_shares:
            rt.confluence_dir = 0 if t_rem < self.s.final_blackout_s else rt.confluence_dir
            return []
        yes_top = rt.books[Side.YES]
        mid = yes_top.mid
        if mid is None:
            return []
        fair = fair_of[Side.YES]
        # model direction: fair vs book mid, beyond threshold
        if fair - mid > self.s.skew_threshold and fair > 0.5 + MIN_FAIR_CONVICTION:
            model_dir = 1
        elif mid - fair > self.s.skew_threshold and fair < 0.5 - MIN_FAIR_CONVICTION:
            model_dir = -1
        else:
            rt.confluence_dir = 0
            return []
        # book lean: depth imbalance on the YES book
        imb = yes_top.imbalance
        book_dir = 1 if imb > self.s.book_imbalance_min else (-1 if imb < -self.s.book_imbalance_min else 0)
        if book_dir != model_dir:
            rt.confluence_dir = 0
            return []
        rt.confluence_dir = model_dir
        side = Side.YES if model_dir > 0 else Side.NO
        top = rt.books[side]
        if top.ask is None:
            return []
        # skew must clear fees too
        if effective_cost(top.ask, m.taker_fee_bps) >= fair_of[side]:
            return []
        step = self.s.skew_step_shares * (1.0 + self.rng.uniform(-self.s.add_jitter_pct,
                                                                 self.s.add_jitter_pct))
        shares = max(1.0, round(min(step, self.s.max_skew_shares - pos.skew_bought), 1))
        return [OrderIntent(
            market_id=m.condition_id, token_id=m.token[side], side=side,
            action=Action.BUY, price=top.ask, shares=shares, signal=SignalType.SKEW,
            reason=f"confluence {'YES' if model_dir > 0 else 'NO'}: fair {fair:.3f} "
                   f"vs mid {mid:.3f}, imb {imb:+.2f}",
        )]

    # ---- staged take-profit on skew shares ----------------------------------
    def _take_profit(self, rt) -> list[OrderIntent]:
        pos = rt.position
        side = pos.skew_side
        if side is None or pos.skew_shares <= 0:
            return []
        top = rt.books[side]
        if top.bid is None:
            return []
        intents = []
        for i, level in enumerate(self.s.tp_levels):
            if level in pos.tp_taken or top.bid < level:
                continue
            # first level sells half the skew, last level sells the rest
            frac = 0.5 if i < len(self.s.tp_levels) - 1 else 1.0
            shares = round(pos.skew_shares * frac, 1)
            if shares < 1:
                continue
            pos.tp_taken.add(level)
            intents.append(OrderIntent(
                market_id=rt.market.condition_id, token_id=rt.market.token[side], side=side,
                action=Action.SELL, price=top.bid, shares=shares,
                signal=SignalType.TAKE_PROFIT, reason=f"TP @ {level:.2f} (bid {top.bid:.3f})",
            ))
        return intents
