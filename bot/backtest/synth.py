"""Synthetic corpus generator — for PIPELINE testing only, not edge validation.

Produces data in the exact recorder JSONL format: BTC 5m/15m up-down windows with a
mix of choppy (OU mean-reverting) and trending (drifting) regimes, an order book that
tracks fair value with a lag (the inefficiency the strategy hunts), spreads, depth
imbalance aligned with the trend, and trade prints. Fees are not modeled.

Usage:
    python -m bot.backtest.synth data/synth --hours 6 --seed 42
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from bot.signal.fair_value import fair_yes

SIGMA_S = 7e-5          # per-sqrt-second log-return vol (~40% annualized)
BOOK_LAG_S = 2          # how far the book's fair value lags true spot
SPREAD = 0.03
TICK = 0.01
TREND_DRIFT = 3.5e-5    # per-second drift in trending windows
CHOP_THETA = 0.05       # OU pull toward strike in choppy windows


def q(p: float) -> float:
    return max(0.01, min(0.99, round(p / TICK) * TICK))


def generate(out_dir: Path, hours: float, seed: int) -> Path:
    rng = random.Random(seed)
    t0 = 1_780_000_000.0
    t0 -= t0 % 900
    horizon = int(hours * 3600)
    events: list[dict] = []

    # ---- true spot path with per-window regimes -----------------------------
    price = 100_000.0
    prices = [price]
    regimes: dict[int, str] = {}   # 5m window start -> regime
    for sec in range(1, horizon + 1):
        w = int(t0 + sec) - int(t0 + sec) % 300
        if w not in regimes:
            regimes[w] = "trend" if rng.random() < 0.4 else "chop"
            regimes[f"{w}_dir"] = rng.choice([1, -1])
            regimes[f"{w}_anchor"] = price
        noise = rng.gauss(0, SIGMA_S)
        if regimes[w] == "trend":
            r = regimes[f"{w}_dir"] * TREND_DRIFT + noise * 0.7
        else:
            anchor = regimes[f"{w}_anchor"]
            r = CHOP_THETA * math.log(anchor / price) / 60 + noise
        price *= math.exp(r)
        prices.append(price)

    def spot(ts: float) -> float:
        i = int(ts - t0)
        return prices[max(0, min(i, len(prices) - 1))]

    # ---- per-second event stream ---------------------------------------------
    ewma5 = ewma30 = 0.0
    markets: list[dict] = []
    for dur, tag in ((300, "5m"), (900, "15m")):
        start = t0
        while start + dur <= t0 + horizon:
            cid = f"synth-{tag}-{int(start)}"
            markets.append({"cid": cid, "dur": dur, "start": start, "end": start + dur,
                            "ty": f"{cid}-Y", "tn": f"{cid}-N"})
            start += dur

    for m in markets:
        events.append({"ts": m["start"] - 60, "type": "market_discovered",
                       "condition_id": m["cid"], "slug": m["cid"], "question": m["cid"],
                       "asset": "BTC", "duration_s": m["dur"],
                       "start_ts": m["start"], "end_ts": m["end"],
                       "token_yes": m["ty"], "token_no": m["tn"],
                       })
        events.append({"ts": m["start"] + 0.5, "type": "strike",
                       "market_id": m["cid"], "strike": spot(m["start"])})
        events.append({"ts": m["end"] + 1, "type": "resolved", "market_id": m["cid"],
                       "close": spot(m["end"]), "strike": spot(m["start"])})

    for sec in range(horizon):
        ts = t0 + sec
        p_now, p_prev = spot(ts), spot(ts - 1)
        r = math.log(p_now / p_prev) if p_prev > 0 else 0.0
        ewma5 = (1 - math.exp(-1 / 5)) * r + math.exp(-1 / 5) * ewma5
        ewma30 = (1 - math.exp(-1 / 30)) * r + math.exp(-1 / 30) * ewma30
        events.append({"ts": ts, "type": "spot_bar", "asset": "BTC", "price": p_now,
                       "bar_ts": int(ts), "sigma": SIGMA_S, "drift": (ewma5 + ewma30) / 2})

        # book updates for each active market (lagged fair value + noise)
        for m in markets:
            if not (m["start"] <= ts < m["end"]):
                continue
            strike = spot(m["start"])
            lagged = spot(ts - BOOK_LAG_S)
            t_rem = m["end"] - ts
            fv = fair_yes(lagged, strike, SIGMA_S, 0.0, t_rem)
            mid = min(0.97, max(0.03, fv + rng.gauss(0, 0.008)))
            w5 = int(ts) - int(ts) % 300
            trending_up = regimes.get(w5) == "trend" and regimes.get(f"{w5}_dir") == 1
            trending_dn = regimes.get(w5) == "trend" and regimes.get(f"{w5}_dir") == -1
            for side, side_mid, tok in (("YES", mid, m["ty"]), ("NO", 1 - mid, m["tn"])):
                bid, ask = q(side_mid - SPREAD / 2), q(side_mid + SPREAD / 2)
                if ask <= bid:
                    ask = q(bid + TICK)
                heavy_bid = (side == "YES" and trending_up) or (side == "NO" and trending_dn)
                bid_depth = rng.uniform(600, 1200) * (1.8 if heavy_bid else 1.0)
                ask_depth = rng.uniform(600, 1200) * (1.0 if heavy_bid else 1.8)
                events.append({"ts": ts + 0.1, "type": "book_top", "market_id": m["cid"],
                               "side": side, "bid": bid, "ask": ask,
                               "bid_size": round(rng.uniform(50, 250), 1),
                               "ask_size": round(rng.uniform(50, 250), 1),
                               "bid_depth": round(bid_depth, 2),
                               "ask_depth": round(ask_depth, 2)})
                if rng.random() < 0.4:  # trade print at bid or ask
                    px = bid if rng.random() < 0.5 else ask
                    events.append({"ts": ts + 0.2, "type": "trade_print",
                                   "market_id": m["cid"], "token": tok, "price": px})

    events.sort(key=lambda e: e["ts"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "events.jsonl"
    with out.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    n_5m = sum(1 for m in markets if m["dur"] == 300)
    n_15m = len(markets) - n_5m
    print(f"wrote {len(events):,} events, {n_5m} x 5m + {n_15m} x 15m windows -> {out}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    generate(a.out_dir, a.hours, a.seed)
