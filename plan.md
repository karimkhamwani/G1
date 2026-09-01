# Polymarket Momentum Arbitrage Bot — Program Plan

A single, self-contained Python program that trades Polymarket's short-duration (5m/15m)
BTC/ETH up-or-down markets by computing a fair YES/NO probability from live spot data
faster than the market's quotes adjust.

> **Reality check:** this documents how to build and validate the system. Profitability is a
> hypothesis the program itself must prove (see [Validation](#8-validation--go-live-gate))
> before real capital is used. Competitors run the same strategy; the edge may not exist.

---

## 1. Core Idea

- Polymarket 5-minute crypto markets resolve on whether the reference price closes above
  the window's open price.
- Spot moves on Binance/Coinbase propagate into Polymarket odds with a lag of seconds.
- The bot holds a **hedged base of equal YES+NO shares** accumulated cheaply on price
  swings, and layers a **directional skew** on top whenever live Binance momentum and
  the Polymarket book agree on a direction. The pairs bound the downside; the skew makes
  the profit when the agreed direction holds.

**Fair value model:**

```
fair_yes = Φ( (spot − strike + drift·t) / (σ · √t) )
```

- `spot − strike` — current deviation from the window's open price
- `t` — seconds remaining in the window
- `σ` — realized volatility (rolling ~30 min of spot trades)
- `drift` — short-horizon momentum term (EWMA of signed trade flow, 5s/30s windows)
- `Φ` — standard normal CDF

Since the market must resolve one way or the other, the NO side is the complement —
one calculation prices both tokens:

```
fair_no = 1 − fair_yes
```

**Strategy: paired accumulation + momentum skew.** Both sides are traded, in two layers:

*Layer 1 — Scaled two-sided accumulation with per-side averaging.*
Enter each market cycle early with a **base of equal YES and NO shares** — no combined-
cost gate on entry; getting positioned matters more than the first fill price. Then
scale in over the life of the window: each side has a ladder of add steps, and when a
side's ask drops meaningfully below that side's **running average cost**, buy another
step and average down. The ladder is sized to the market duration — a 5-minute market
gets fewer, faster steps; a 15-minute market gets more steps spread across more swings.

Per side, track `shares` and `avg_cost` (volume-weighted). The position's health metric
is the **combined average cost**:

```
combined_avg = avg_yes_cost + avg_no_cost
matched_profit_at_resolution = min(yes_shares, no_shares) × (1.00 − combined_avg)
```

Combined average below $1 is the *goal by resolution* (achieved by buying the swings),
not an entry requirement. Scaling rules keep it honest: adds go only to the side that
improves its average (never average *up*), step sizes shrink as the window ages, and
all adds stop at the per-side and per-market share caps.

Two gates protect Layer 1 from its own failure mode (buying the losing side just
because it got cheap):

- **Model gate** — an add fires only if the side is also cheap *versus fair value*
  (`ask ≤ fair_side − add_margin`), not merely below our own average cost. A falling
  price that the model agrees with is a losing side at a fair price; skip it.
- **Regime gate** — a live chop/trend classifier per window
  (`chop_score = price range ÷ |net move|` over the window so far, plus drift
  persistence). Ladder adds are enabled only while the window classifies as choppy;
  in a trending window Layer 1 freezes (base position only) and only Layer 2 may act.
  This is the direct defense against the strategy's worst case: averaging into a
  one-way move.

*Layer 2 — Momentum skew (the profit engine).*
Continuously compare the Binance-implied direction against the Polymarket book:

```
model_says   = sign(fair_yes − 0.5)          # from live spot + drift
book_says    = sign(book imbalance / mid drift)
confluence   = both agree, and |fair_yes − book_mid| > skew_threshold
```

When Binance momentum and the book **agree** on a direction, buy extra shares on that
side only — deliberate *unmatched* exposure in the "realistic" direction, added in steps
as confluence persists, up to `max_skew_shares`. The paired base hedges the whipsaw;
the skew captures the move:

```
total_pnl = pair_profit + skew_shares × (payout − avg_skew_cost)
```

If the skewed side wins, skew shares pay $1 each; if it loses, the loss is bounded by
the skew's cost and partially offset by pair profit. All accumulation stops at
`max_shares_per_market`.

---

## 2. Architecture

One asyncio process, five long-running tasks communicating through in-memory queues,
with all state journaled to disk.

```
┌─────────────────────────────────────────────────────────────┐
│                        bot (asyncio)                        │
│                                                             │
│  MarketDiscovery ──┐                                        │
│                    ├──► SignalEngine ──► Executor ──► CLOB  │
│  SpotFeed ─────────┤          │             │               │
│                    │          ▼             ▼               │
│  BookFeed ─────────┘     RiskManager ◄── PositionTracker    │
│                               │                             │
│                          KillSwitch                         │
│                                                             │
│  Recorder (taps every feed + every decision → disk)         │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Modules

### 3.1 `discovery/` — Market Discovery
- The short-duration markets have deterministic slugs —
  `{asset}-updown-{5m|15m}-{windowStartUnix}` — so discovery constructs the slug for
  the current and next window of each configured `ASSETS` × `DURATIONS` series and
  fetches each directly from the Gamma API (no listing scans). The window open is
  `eventStartTime`; `startDate` is market creation time, not the window.
- Extracts: condition ID, YES/NO token IDs, strike (window open price), open/close
  timestamps, resolution source.
- Maintains a registry of live market cycles; emits `MarketOpened` / `MarketClosed` events.

### 3.2 `feeds/` — Market Data
- **SpotFeed**: Binance websocket trade stream (BTC/ETH). Maintains last price, rolling
  realized volatility, and momentum EWMAs. Flags itself stale if no tick for > 2 s.
- **BookFeed**: Polymarket CLOB websocket (`wss://ws-subscriptions-clob.polymarket.com`)
  per active market. Maintains full L2 book for YES and NO tokens: best bid/ask, depth,
  imbalance ratio.
- **OracleFeed**: the actual resolution price source (verify current oracle — Chainlink/
  Pyth-based). Used for resolution prediction near window close; Binance alone is not
  authoritative.
- All feeds auto-reconnect with backoff and publish heartbeats the RiskManager watches.

### 3.3 `signal/` — Signal Engine
- On every spot tick or book update for an active market, emit up to three signal types:
  1. **`BaseEntrySignal`** — early in the cycle (within `entry_window_s` of open, spread
     sane): buy `base_shares` of each side. No combined-cost gate — the goal is to be
     positioned before the window's swings, not to nail the first price.
  2. **`ScaleAddSignal`** — fires only when ALL of: (a) the side's best ask is at least
     `add_trigger_drop` below its running average cost, (b) **model gate**: the ask is
     also below fair value by `add_margin` (fees included), (c) **regime gate**: the
     window currently classifies as choppy. Buys `add_step_shares` (± random jitter in
     size and price so the ladder doesn't telegraph a repeating pattern to the book).
     Governed by the duration-scaled ladder: at most `max_adds_per_side` adds, step
     spacing and sizing derived from the market length (5m → few fast steps, 15m → more,
     wider), steps shrinking as time remaining decays. Never adds to a side above its
     own average (no averaging up through Layer 1).
  3. **`SkewSignal`** — confluence detected: Binance-implied direction
     (`fair_yes` vs 0.5, from spot + drift) and the book's own lean (imbalance + mid
     drift) agree, and the gap exceeds `skew_threshold`. Buy `skew_step_shares` on the
     favored side; repeatable while confluence persists, up to `max_skew_shares`.
     Confluence lost → stop adding (optionally trim skew into strength).
- **Regime classifier** — maintained per active window from spot ticks: chop score,
  drift persistence, and realized vol. Published with every signal so the backtest can
  split results by regime.
- **All edge math includes fees** — taker/maker fees for these specific markets are
  fetched from the CLOB API at startup (and re-checked periodically); every threshold
  is applied net of fees, never gross.
- Vetoes applied to all signals: stale feed, book depth below floor, final-seconds
  blackout for new skew (pairs may still complete), max spread filter,
  `max_shares_per_market` cap.
- Pure function of inputs → deterministic, unit-testable, and replayable in backtests.

### 3.4 `execution/` — Executor & Position Tracker
- Live trading goes through the **Polymarket CLOB v2 API** (REST + websocket at
  `clob.polymarket.com`), using the official `py-clob-client` with L2 auth — the
  wallet's private key derives API key/secret/passphrase once, and all order
  placement/cancellation is signed with those credentials.
- Places **limit orders only**, priced so that fill implies the required edge; never
  crosses into thin books with market orders.
- Per-market state machine:
  `IDLE → SIGNAL → ORDER_PLACED → PARTIAL/FILLED/EXPIRED → AWAITING_RESOLUTION → REDEEMED`
- Subscribes to the CLOB v2 **user channel** websocket for real-time order status and
  fill events (no polling); REST is the fallback for reconciliation on reconnect.
- Cancels resting orders when the signal decays below threshold, and **fast-cancels on
  adverse spot moves** — if spot moves against a resting bid beyond a threshold, pull it
  immediately rather than letting informed flow fill it (the main defense against
  adverse selection).
- Staged exits: take-profit levels sell into strength before resolution when the book
  allows; otherwise hold to resolution.
- **Position accounting is per-side averages**: the tracker maintains, per market cycle,
  each side's `shares` and volume-weighted `avg_cost`, plus the derived views —
  `matched = min(yes_shares, no_shares)` (locked cash redeeming at $1, profitable iff
  `combined_avg < 1`) and `skew` (the unmatched remainder — the only directional
  exposure). Every fill updates the averages and the ladder state (adds used, next
  trigger levels).
- Automated redemption of winning positions after market resolution.
- **Idempotent restart**: on boot, reconstructs open orders and positions from the
  exchange, never from local memory alone.

### 3.5 `risk/` — Risk Manager & Kill Switch
Hard limits enforced outside the strategy logic (the strategy cannot override them):
- max USDC per market cycle
- max concurrent markets
- max total exposure (including capital locked awaiting redemption; matched YES/NO
  pairs count as locked capital, not directional risk)
- max daily loss → **kill switch**: cancel all orders, flatten sellable positions, halt
- consecutive-error and feed-staleness halts
- **Disconnect fail-safe**: on feed or CLOB connection loss beyond `feed_stale_s` while
  holding open exposure, immediately cancel all resting orders (the cancel path is the
  one thing kept maximally reliable); on reconnect, reconcile positions/orders from the
  exchange before emitting any new signal.
- Every veto and halt is logged with its reason.

### 3.6 `record/` — Recorder
- Taps every feed message, signal, order event, and fill to disk (append-only JSONL,
  partitioned by UTC day — crash-safe). This corpus powers backtesting and post-mortems.
- Runs identically in paper and live modes.

### 3.7 `backtest/` — Replay & Simulation
- Replays recorded data through the *same* SignalEngine code (no reimplementation).
- **Queue-realistic fill model**: a resting limit order fills only when the recorded
  book *trades through* its price level (not merely touches it), capped at traded
  volume, with a 200–500 ms latency penalty and all fees included. Paper mode uses the
  same fill model live.
- Models capital lockup from redemption delays (caps concurrent cycles).
- **Walk-forward validation**: parameters may be tuned only on the first half of the
  corpus; results are reported on the untouched second half. Re-tuning on data that
  produced a failing result is prohibited by process.
- Required report outputs (these answer the strategy's fatal questions directly):
  - **P&L split by regime** — choppy vs. trending windows, separately for Layer 1 and
    Layer 2 (tests whether the ladder really earns in chop and how much it bleeds in
    trends);
  - **fill-rate calibration** — achieved fills vs. model-assumed fills, and later paper
    vs. live (tests adverse selection and simulator honesty);
  - achieved combined average cost per window (distribution, not just mean);
  - expectancy per trade, hit rate, max drawdown, PnL by time-remaining bucket;
  - **ROI arithmetic** — expected $/day at achievable size given observed book depth,
    stated plainly so the effort-vs-return decision is explicit.

### 3.8 `monitor/` — Dashboard & Observability
A local web dashboard (FastAPI serving one self-contained page, live-updated over a
websocket — no external services) at `http://localhost:${DASHBOARD_PORT}`:

- **Header strip** — mode (PAPER/LIVE badge), session P&L, total exposure, capital
  locked awaiting redemption, daily-loss headroom vs kill switch, feed health lights
  (spot / book / user channel, with latency ms).
- **Active market cards** — one card per open cycle: countdown to resolution, live
  YES/NO best bid/ask, per-side shares + avg cost, **combined average cost vs $1**,
  matched vs skew breakdown, ladder state (adds used per side), current confluence
  reading (Binance direction vs book lean), and unrealized P&L for the cycle.
- **Live fills tape** — scrolling feed of every order event: placed, filled, partial,
  cancelled, expired, with price/size/side and which signal triggered it.
- **Session charts** — equity curve, P&L per market cycle (won/lost bars, choppy vs
  trending tagged), achieved combined avg cost histogram.
- **History table** — resolved cycles with per-cycle stats, filterable, exportable CSV.
- **Controls** — pause new entries, flatten-and-halt (manual kill switch). Live mode
  only; every action confirmed and logged.

Identical in paper and live mode (paper shows simulated fills). Underneath it: the same
structured JSON logs of every decision (signal, veto, order, fill, halt) and Telegram
alerts on kill-switch, feed loss, or crash.

---

## 4. Run Modes

One binary, one flag — identical code path in both:

| Mode      | Feeds | Signals | Orders                    |
|-----------|-------|---------|---------------------------|
| `paper`   | live  | live    | simulated fills, logged   |
| `live`    | live  | live    | real, risk-gated          |

The Recorder runs in both modes, so paper trading doubles as data collection — the
recorded feeds from paper runs are the backtest corpus.

---

## 5. Project Layout

```
polymarket-momentum-bot/
├── plan.md
├── pyproject.toml
├── .env                     # ALL configuration: market slug, thresholds, limits,
│                            # wallet key, API creds (gitignored)
├── .env.example             # same keys with safe defaults, committed as documentation
├── bot/
│   ├── main.py              # entrypoint: MODE=paper|live (from .env)
│   ├── discovery/
│   ├── feeds/
│   ├── signal/
│   ├── execution/
│   ├── risk/
│   ├── record/
│   ├── backtest/
│   └── monitor/             # FastAPI dashboard (static/index.html + ws state feed)
├── data/                    # recorded feeds (gitignored)
└── tests/                   # unit tests for signal math + state machines
```

**Stack:** Python 3.11+, `asyncio`, `websockets`, `aiohttp`, `py-clob-client`
(Polymarket's official CLOB v2 client, live mode only), stdlib math (Φ via `erf`),
`pydantic-settings` (config), `fastapi`/`uvicorn` (dashboard). JSONL recording, no
heavy data deps.

---

## 6. Configuration — everything in `.env`

All configuration lives in a single `.env` file (loaded via `pydantic-settings`), so the
market slug, every tunable, and the credentials are defined in one place. A committed
`.env.example` documents the keys; the real `.env` is gitignored.

```dotenv
# ── Market selection ─────────────────────────────────────────
# Slugs constructed as {asset}-updown-{5m|15m}-{windowStartUnix} from ASSETS+DURATIONS
ASSETS=BTC,ETH
DURATIONS=5m,15m

# ── Run mode ─────────────────────────────────────────────────
MODE=paper                         # paper | live

# ── Signal ───────────────────────────────────────────────────
VOL_WINDOW_MIN=30
MOMENTUM_EWMA_S=5,30
FINAL_BLACKOUT_S=20                # no new skew entries in last N seconds

# ── Scaling (Layer 1: two-sided accumulation) ────────────────
BASE_SHARES=20                     # initial equal YES+NO position per market
ENTRY_WINDOW_S=30                  # place base entry within N seconds of market open
ADD_TRIGGER_DROP=0.05              # add to a side when ask < that side's avg_cost − this
ADD_MARGIN=0.03                    # model gate: ask must also be ≤ fair − this (net of fees)
ADD_STEP_SHARES=10                 # shares per averaging add
ADD_JITTER_PCT=0.25                # randomize step size/price ±25% (don't telegraph the ladder)
CHOP_SCORE_MIN=2.0                 # regime gate: range ÷ |net move| must exceed this for adds
MAX_ADDS_PER_SIDE_5M=3             # ladder scales with market length
STEP_DECAY_5M=0.7
MAX_ADDS_PER_SIDE_15M=6
STEP_DECAY_15M=0.85
MAX_SHARES_PER_SIDE=60

# ── Skew (Layer 2: momentum direction) ───────────────────────
SKEW_THRESHOLD=0.05                # min |fair_yes − book_mid| with confluence
SKEW_STEP_SHARES=10                # shares added per confluence step
MAX_SKEW_SHARES=40                 # cap on unmatched directional shares

# ── Risk ─────────────────────────────────────────────────────
MAX_SHARES_PER_MARKET=120          # total shares (both sides + skew) per cycle
MAX_PER_MARKET_USDC=25
MAX_CONCURRENT_MARKETS=4
MAX_TOTAL_EXPOSURE_USDC=150
MAX_DAILY_LOSS_USDC=40             # kill switch
FEED_STALE_S=2

# ── Dashboard ────────────────────────────────────────────────
DASHBOARD_PORT=8080                # local web dashboard (0 to disable)
DASHBOARD_HOST=127.0.0.1           # bind local only; never expose publicly

# ── Execution ────────────────────────────────────────────────
ORDER_TTL_S=10
MIN_BOOK_DEPTH_USDC=200
TAKE_PROFIT_LEVELS=0.90,0.97
FAST_CANCEL_SPOT_MOVE=0.0008       # pull resting bids if spot moves this fraction against them
FEE_REFRESH_MIN=30                 # re-fetch market fee schedule every N minutes

# ── Endpoints (Polymarket CLOB v2) ───────────────────────────
CLOB_HOST=https://clob.polymarket.com     # CLOB v2 REST (orders, cancels, balances)
CLOB_WS=wss://ws-subscriptions-clob.polymarket.com   # CLOB v2 market/user channels
CHAIN_ID=137                              # Polygon mainnet
GAMMA_HOST=https://gamma-api.polymarket.com
BINANCE_WS=wss://stream.binance.com:9443

# ── Credentials (never committed) ────────────────────────────
POLYGON_WALLET_PRIVATE_KEY=
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
TELEGRAM_BOT_TOKEN=                # optional: alerts
TELEGRAM_CHAT_ID=
```

A typed `Settings` class (pydantic-settings) parses and validates every variable at
startup — bad or missing values fail fast before any connection is opened.

---

## 7. Security

- Dedicated hot wallet holding only working capital; key in `.env`, never committed.
- Withdraw profits out of the hot wallet on a schedule; the bot never holds more than
  `max_total_exposure` + buffer.
- No third-party code from unaudited "bot" repos.

---

## 8. Validation & Go-Live Gate

The program must pass these gates **in order**; failure at any gate means stop or revise
the model — never "tune until profitable" on the same dataset.

0. **Pre-build viability check** (an afternoon, before writing the bot): confirm the
   current fee schedule for 5m/15m crypto markets from the CLOB API, eyeball typical
   spreads and book depth, and do the ROI arithmetic — max edge × windows/day ×
   achievable size − fees. If the best case is negligible, decide consciously whether
   this is a learning project or an income project before investing the build time.
1. **Paper trade** ≥ 1–2 weeks — this simultaneously records the full feed corpus and
   produces simulated P&L (queue-realistic fills, fees included).
2. **Backtest** on the recorded data, walk-forward (tune on half, judge on the untouched
   half) → positive expectancy after all costs over ≥ 500 trades, drawdown within
   tolerance, **Layer 1 profitable in choppy windows and its trending-window bleed
   smaller than the chop profit**, and fill-rate calibration within tolerance.
3. **Live micro-capital** ($100–200 total) ≥ 2 weeks → live PnL consistent with paper,
   and live fill rates consistent with the simulator (if live fills are much worse,
   adverse selection is eating the edge — stop and reassess).
4. Scale in steps only while live results keep matching expectation; re-run this gate
   whenever performance degrades (edges decay as competitors adapt).

---

## 9. Known Risks

- **Latency competition** — colocated bots see the same signal first; retail latency may
  kill the thesis entirely (this is what the gates test).
- **Adverse selection** — resting limit orders fill precisely when the move reverses.
  Mitigated by fast-cancel on adverse spot moves and short order TTLs; measured by the
  fill-rate calibration report (paper vs. live fill quality is the tell).
- **Pair-cost headwind** — in a normal book, buying both sides costs slightly *over* $1
  (you pay the spread twice). The combined average only gets under $1 if swings actually
  trigger the ladder; in a calm window the ungated base entry locks in a small loss on
  matched shares, and the skew must carry the whole P&L. The backtest's key output is
  the achieved combined average cost per window.
- **Averaging into the trend** — ladder adds go to the falling side, which in a one-way
  trending window is the *losing* side. The model gate and regime gate exist precisely
  to block this (an add now requires the model to call the side mispriced AND the window
  to classify as choppy), but both gates can misclassify — a trend that starts mid-window
  looks like chop at first. The regime-split backtest report is the test of whether the
  gates actually hold the line.
- **Skew whipsaw** — the momentum skew is plain directional risk; confluence signals can
  flip seconds before resolution, and the skew loss can exceed accumulated pair profit.
- **Oracle mismatch** — Binance says up, the resolution feed says down.
- **Reversal risk** — spot whipsaws inside the window after entry.
- **Capital lockup** — redemption delay limits cycle throughput and compounds drawdowns.
- **Regime change** — Polymarket fee/oracle/market-structure changes can invalidate the
  model overnight.
