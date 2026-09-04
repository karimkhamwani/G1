"""Signal engine: BaseEntry, ScaleAdd (model + regime gated), Skew (confluence), TakeProfit.

Pure evaluation over shared state — no I/O — so the same code runs live, in paper
mode, and in backtest replay.
"""
from __future__ import annotations

import logging
import math
import random
import time

from bot.models import Action, OrderIntent, Side, SignalType
from bot.signal.fair_value import fair_yes
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
        self._vol_warned: dict[str, bool] = {}   # asset -> already noted "no vol estimate"

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
        if sigma <= 0.0:
            # No trustworthy volatility estimate. fair_yes would divide by the MIN_SIGMA
            # floor and saturate to ~0/~1, turning sub-basis-point noise into false
            # conviction — which is exactly how the skew layer lost money. Take-profit
            # still runs below: it reduces risk and does not depend on the model.
            rt.fair_yes = None
            rt.confluence_dir = 0
            if not self._vol_warned.get(m.asset):
                self._vol_warned[m.asset] = True
                self.hub.note(f"{m.asset}: no usable volatility estimate "
                              f"(feed gaps) - entries paused")
            return self._submit(rt, self._take_profit(rt), None, None)
        self._vol_warned[m.asset] = False
        fair = fair_yes(spot.price, m.strike, sigma, drift, t_rem)
        rt.fair_yes = fair
        fair_of = {Side.YES: fair, Side.NO: 1.0 - fair}

        regime = classify(spot.prices_since(m.start_ts), m.strike, self.s.chop_score_min,
                          self.s.min_net_move_frac)
        rt.regime = regime

        intents: list[OrderIntent] = []
        if self.s.strategy == "directional":
            intents += self._directional(rt, fair_of, t_rem)
        else:
            intents += self._base_entry(rt, now)
            intents += self._scale_adds(rt, now)
            intents += self._skew(rt, fair_of, t_rem)
        intents += self._take_profit(rt)

        return self._submit(rt, intents, fair, regime, sigma, drift, spot.price, t_rem)

    # ---- directional strategy: one-sided conviction bets --------------------------
    def _directional(self, rt, fair_of: dict, t_rem: float) -> list[OrderIntent]:
        """Two conditions, both required: model confidence on a side >= DIR_CONF_MIN
        AND that side's ask >= DIR_MIN_ENTRY_PRICE (the book backs it too). First bet
        DIR_STEP_SHARES; further
        bets only while the threshold still holds and confidence has risen by
        DIR_CONF_STEP since the last fill. Total spend capped at DIR_MARKET_BUDGET_USDC.
        FOK orders: an unfilled bet dies and is re-placed at the fresh ask after the
        backoff, as long as the threshold still holds. One side per market, held to
        resolution."""
        m, pos = rt.market, rt.position
        # STOP-LOSS first: if we hold a side and BOTH the book and the model have
        # collapsed on it (ask and confidence <= DIR_EXIT_BELOW), sell everything
        held = pos.skew_side   # the side we hold (directional positions are one-sided)
        if held is not None and pos.shares[held] > 0 and not pos.exit_pending:
            top_h = rt.books[held]
            if (top_h.ask is not None and top_h.ask <= self.s.dir_exit_below
                    and fair_of[held] <= self.s.dir_exit_below):
                if top_h.bid is None:
                    return []   # nothing to sell into right now; retry next eval
                pos.exit_pending = True
                shares = round(pos.shares[held], 2)
                return [OrderIntent(
                    market_id=m.condition_id, token_id=m.token[held], side=held,
                    action=Action.SELL, price=top_h.bid, shares=shares,
                    signal=SignalType.STOP_LOSS,
                    reason=f"stop-loss: ask {top_h.ask:.2f} and conf {fair_of[held]:.2f} "
                           f"<= {self.s.dir_exit_below:.2f}, selling all {shares:g}",
                )]
        if pos.stopped_out or pos.exit_pending:
            rt.confluence_dir = 0
            return []   # bailed out of this market: never re-enter it
        if t_rem < self.s.final_blackout_s:
            rt.confluence_dir = 0
            return []
        # condition 1: which side (if any) does the model back with >= DIR_CONF_MIN
        if fair_of[Side.YES] >= self.s.dir_conf_min:
            side = Side.YES
        elif fair_of[Side.NO] >= self.s.dir_conf_min:
            side = Side.NO
        else:
            rt.confluence_dir = 0
            return []
        # never build the opposite side too (that would be a pair, not a bet)
        if pos.shares[side.other] > 0:
            rt.confluence_dir = 0
            return []
        top = rt.books[side]
        conf = fair_of[side]
        # condition 2: the book must agree — its price for the side is at/above the floor
        if top.ask is None or top.ask < self.s.dir_min_entry_price:
            rt.confluence_dir = 0
            return []
        # optional extra gate (off by default): whichever crossed its threshold first,
        # both above threshold is the entry — the book may well be ahead of the model
        if self.s.dir_require_edge and top.ask >= conf:
            rt.confluence_dir = 0
            return []
        rt.confluence_dir = 1 if side is Side.YES else -1
        if self._pending(m.condition_id, side) > 0.5:
            return []   # a bet is working (or backing off after a kill)
        # after the first fill, the threshold must be HELD and confidence must have
        # RISEN a little for the next bet
        if pos.bought[side] > 0 and conf < pos.dir_last_fair + self.s.dir_conf_step:
            return []
        # budget: real spend (fills) + anything working, hard-capped per market
        pending_notional = self._pending(m.condition_id, side) * top.ask
        remaining = self.s.dir_market_budget_usdc - pos.cost_basis - pending_notional
        shares = float(int(min(self.s.dir_step_shares, remaining // top.ask)))
        if shares < self.s.min_order_shares or shares * top.ask < 1.0:
            return []   # what's left of the budget can't make a valid order
        pos.dir_pending_fair = conf   # promoted to dir_last_fair when this bet fills
        return [OrderIntent(
            market_id=m.condition_id, token_id=m.token[side], side=side,
            action=Action.BUY, price=top.ask, shares=shares,
            signal=SignalType.DIRECTIONAL,
            reason=f"conf {conf:.2f} >= {self.s.dir_conf_min:.2f}, ask {top.ask:.2f} >= "
                   f"{self.s.dir_min_entry_price:.2f}, spent {pos.cost_basis:.2f}/"
                   f"{self.s.dir_market_budget_usdc:.0f}",
        )]

    def _submit(self, rt, intents, fair, regime, sigma=0.0, drift=0.0,
                spot_price=None, t_rem=0.0) -> list[OrderIntent]:
        """Risk-check, log and dispatch intents. Shared by the normal path and the
        no-volatility path (which still runs take-profit)."""
        m = rt.market
        submitted = []
        for intent in intents:
            if intent.shares < self.s.min_order_shares:
                self.recorder.log("veto", {"market_id": m.condition_id,
                                           "signal": intent.signal.value,
                                           "why": "below_min_order_size"})
                continue
            # marketable orders need >= $1 notional (a 5-share buy at 17c is $0.85 and
            # gets rejected) — bump the share count to clear it
            if intent.action is Action.BUY and intent.notional < 1.0:
                intent.shares = float(math.ceil(1.02 / intent.price))
            verdict = self.risk.validate(intent, rt)
            if verdict is None:
                self.recorder.log("signal", {
                    "market_id": m.condition_id, "signal": intent.signal.value,
                    "side": intent.side.value, "action": intent.action.value,
                    "price": intent.price, "shares": round(intent.shares, 2),
                    "fair_yes": round(fair, 4) if fair is not None else None,
                    "regime": regime.label if regime else "unknown",
                    "chop": round(regime.chop_score, 2) if regime else None,
                    "reason": intent.reason,
                    # model diagnostics — needed to debug fair-value quality offline
                    "sigma": round(sigma, 8), "drift": round(drift, 8),
                    "spot": spot_price, "strike": m.strike, "t_rem": round(t_rem, 1),
                })
                if intent.action is Action.BUY:
                    rt.position.ordered[intent.side] += intent.shares
                self.executor.submit(intent)
                submitted.append(intent)
            else:
                self.recorder.log("veto", {"market_id": m.condition_id,
                                           "signal": intent.signal.value, "why": verdict})
        return submitted

    # ---- pending-order gate ---------------------------------------------------
    def _pending(self, market_id: str, side: Side) -> float:
        """Shares still open at the executor for this market/side — blocks a trigger
        from re-firing during order latency (the duplicate-order bug). Also treats a
        side in post-failure backoff as pending, so a still-true trigger doesn't
        re-fire every eval into the same rejection."""
        rt = self.hub.markets.get(market_id)
        if rt and self.clock() < rt.position.blocked_until.get(side, 0.0):
            return 1.0
        open_shares = getattr(self.executor, "open_shares", None)
        return open_shares(market_id, side) if open_shares else 0.0

    # ---- layer 1: base entry ------------------------------------------------
    def _base_entry(self, rt, now: float) -> list[OrderIntent]:
        m, pos = rt.market, rt.position
        if now - m.start_ts > self.s.entry_window_s * 2:
            return []
        if pos.base_placed:
            return self._base_leg_repair(rt, now)
        intents = []
        for side in Side:
            top = rt.books[side]
            if top.ask is None or top.spread is None or top.spread > MAX_SPREAD:
                return []   # need both books sane before committing either leg
            if top.ask_depth_usdc + top.bid_depth_usdc < self.s.min_book_depth_usdc:
                return []
            pos.base_price[side] = top.ask
            intents.append(OrderIntent(
                market_id=m.condition_id, token_id=m.token[side], side=side,
                action=Action.BUY, price=top.ask, shares=float(round(self.s.base_shares)),
                signal=SignalType.BASE_ENTRY, reason="base two-sided entry",
            ))
        pos.base_placed = True
        return intents

    def _base_leg_repair(self, rt, now: float) -> list[OrderIntent]:
        """A base leg whose order died unfilled (TTL/cancel) leaves unintended one-sided
        exposure — re-place it once at the current ask."""
        m, pos = rt.market, rt.position
        intents = []
        for side in Side:
            if pos.bought[side] > 0 or pos.base_retried[side]:
                continue
            if self._pending(m.condition_id, side) > 0.5:
                continue   # original order still working
            top = rt.books[side]
            if top.ask is None:
                continue
            # Repair re-places the leg at the CURRENT ask, so after a move it can pay
            # far more than intended (a 0.52 leg was once repaired at 0.80, which alone
            # pushed that cycle's combined average to 1.23). Abandon instead of chasing.
            quoted = pos.base_price[side]
            if quoted and top.ask > quoted + self.s.repair_max_slip:
                pos.base_retried[side] = True
                self.hub.note(f"base leg repair abandoned ({side.value} quoted "
                              f"{quoted:.2f}, now {top.ask:.2f}) - not chasing")
                continue
            pos.base_retried[side] = True
            intents.append(OrderIntent(
                market_id=m.condition_id, token_id=m.token[side], side=side,
                action=Action.BUY, price=top.ask, shares=float(round(self.s.base_shares)),
                signal=SignalType.BASE_ENTRY,
                reason="base leg repair (first order died unfilled)",
            ))
        return intents

    # ---- layer 1: ladder adds (model gate + regime gate) ---------------------
    def _scale_adds(self, rt, now: float) -> list[OrderIntent]:
        """Layer 1 ladder: add MATCHED PAIRS — both sides at once, equal shares.

        A matched pair pays exactly $1 at resolution no matter which way BTC goes, so
        a pair bought for less than $1 is locked profit and needs no directional model
        or regime call. Trigger: the combined ask is below PAIR_ADD_MAX, or below our
        own combined average by ADD_TRIGGER_DROP (improving the average)."""
        m, pos = rt.market, rt.position
        if not pos.base_placed:
            return []
        # window-age gate: no adds until the window has shown some character
        if now - m.start_ts < self.s.min_window_age_s:
            return []
        yes, no = rt.books[Side.YES], rt.books[Side.NO]
        if yes.ask is None or no.ask is None:
            return []
        pair_ask = yes.ask + no.ask
        cavg = pos.combined_avg
        cheap_vs_avg = cavg is not None and pair_ask <= cavg - self.s.add_trigger_drop
        if not (pair_ask <= self.s.pair_add_max or cheap_vs_avg):
            return []
        max_adds, decay = self.s.ladder(m.duration_s)
        adds_done = max(pos.adds_used[Side.YES], pos.adds_used[Side.NO])
        if adds_done >= max_adds:
            return []
        # both legs must be clear: a working order on either side blocks the pair
        if (self._pending(m.condition_id, Side.YES) > 0.5
                or self._pending(m.condition_id, Side.NO) > 0.5):
            return []
        step = self.s.add_step_shares * (decay ** adds_done)
        step *= 1.0 + self.rng.uniform(-self.s.add_jitter_pct, self.s.add_jitter_pct)
        # WHOLE shares (FAK precision rule), and sized so the CHEAPER leg clears the
        # $1 minimum notional — otherwise _submit would bump one leg and unbalance
        # the pair
        min_for_notional = math.ceil(1.02 / max(min(yes.ask, no.ask), 0.01))
        shares = float(max(self.s.min_order_shares, min_for_notional, round(step)))
        if (pos.shares[Side.YES] + shares > self.s.max_shares_per_side
                or pos.shares[Side.NO] + shares > self.s.max_shares_per_side):
            return []
        reason = f"pair add: sum {pair_ask:.3f}" + (f", c.avg {cavg:.3f}" if cavg else "")
        return [
            OrderIntent(market_id=m.condition_id, token_id=m.token[side], side=side,
                        action=Action.BUY, price=min(top.ask, 0.99), shares=shares,
                        signal=SignalType.SCALE_ADD, reason=reason)
            for side, top in ((Side.YES, yes), (Side.NO, no))
        ]

    # ---- layer 2: momentum skew ------------------------------------------------
    def _skew(self, rt, fair_of: dict, t_rem: float) -> list[OrderIntent]:
        m, pos = rt.market, rt.position
        if not self.s.skew_enabled:
            rt.confluence_dir = 0
            return []
        # Same fail-closed discipline as the ladder: a directional, unhedged bet needs
        # a classified market. Too few in-window bars means the fair value rests on a
        # handful of prints, which is how sub-basis-point noise became false conviction.
        if rt.regime is None or not rt.regime.known:
            rt.confluence_dir = 0
            return []
        # LATE-WINDOW ONLY: the skew is a conviction trade near resolution, when the
        # move is largely decided — not an all-window momentum chase
        if not (self.s.final_blackout_s <= t_rem <= self.s.skew_window_s):
            rt.confluence_dir = 0
            return []
        if pos.skew_bought >= self.s.max_skew_shares:
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
        # conviction floor: this close to resolution the model saturates when spot is
        # clearly away from the strike — anything less is noise, not confidence
        if fair_of[side] < self.s.skew_min_fair:
            rt.confluence_dir = 0
            return []
        top = rt.books[side]
        if top.ask is None:
            return []
        # churn guard: once take-profit has fired, never rebuild the skew (the log
        # showed a sell @0.90 followed by a rebuy @0.91 — pure fee churn)
        if pos.tp_taken:
            return []
        if self._pending(m.condition_id, side) > 0.5:
            return []      # skew order already working — don't double-fire
        # the ask must still be below fair value (fees are not modeled — keep
        # SKEW_THRESHOLD wide enough to cover them)
        if top.ask >= fair_of[side]:
            return []
        step = self.s.skew_step_shares * (1.0 + self.rng.uniform(-self.s.add_jitter_pct,
                                                                 self.s.add_jitter_pct))
        shares = float(round(min(step, self.s.max_skew_shares - pos.skew_bought)))  # whole shares
        if shares < self.s.min_order_shares:
            return []   # remaining cap is below the exchange minimum order size
        # respect the per-side cap here instead of spamming risk vetoes
        if pos.shares[side] + shares > self.s.max_shares_per_side:
            return []
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
        if self.clock() < pos.blocked_until.get(side, 0.0):
            return []   # backing off after a failed sell — don't spam re-fires
        if self._pending(rt.market.condition_id, side) > 0.5:
            return []
        top = rt.books[side]
        if top.bid is None:
            return []
        intents = []
        if pos.skew_shares < self.s.min_order_shares:
            return []   # too small to sell: the exchange rejects sub-minimum orders
        for i, level in enumerate(self.s.tp_levels):
            if level in pos.tp_taken or top.bid < level:
                continue
            # first level sells half the skew, last level sells the rest —
            # bumped to the exchange minimum (a 2.5-share sell gets rejected)
            frac = 0.5 if i < len(self.s.tp_levels) - 1 else 1.0
            # whole shares, floored so we never sell more than we hold
            shares = float(int(min(max(pos.skew_shares * frac, self.s.min_order_shares),
                                   pos.skew_shares)))
            if shares < self.s.min_order_shares:
                continue
            intent = OrderIntent(
                market_id=rt.market.condition_id, token_id=rt.market.token[side], side=side,
                action=Action.SELL, price=top.bid, shares=shares,
                signal=SignalType.TAKE_PROFIT, reason=f"TP @ {level:.2f} (bid {top.bid:.3f})",
            )
            # consumed now to block re-fire, but tracked as pending: if the sell dies
            # unfilled the executor's order_closed callback re-arms the level
            pos.tp_taken.add(level)
            pos.tp_pending[intent.id] = level
            intents.append(intent)
        return intents
