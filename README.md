# polymarket-momentum-bot

Two-layer bot for Polymarket's short-duration (5m/15m) BTC/ETH up-or-down markets:

- **Layer 1 — paired accumulation**: equal YES+NO base entered at window open, then
  ladder adds that average each side down on swings — gated by a fair-value model AND a
  chop/trend regime classifier so it never averages into a one-way move.
- **Layer 2 — momentum skew**: extra shares on one side only when live Binance momentum
  and the Polymarket order book agree on a direction.

Full design, strategy math, failure modes, and validation gates: [plan.md](plan.md).
**Read §8 (Validation & Go-Live Gate) before considering live mode.**

> ⚠ Real-world fee check (2026-09): these markets report a **1000 bps (10%) taker base
> fee** — fee = 0.10 × min(p, 1−p) per share. All edge math in the bot is net of this,
> which will veto many marginal trades. That is correct behavior, not a bug.

## Setup — Windows

```bat
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
copy .env.example .env
```

macOS/Linux: `python3 -m venv .venv && .venv/bin/pip install -e .` and `cp`.

Edit `.env` — every variable lives there (assets, durations, all strategy tunables,
risk caps, dashboard port). `MODE=paper` is the default and the only sane starting
point.

## Run

```bat
.venv\Scripts\python -m bot.main
```

Dashboard: <http://127.0.0.1:8080> — live market cards (per-side averages, combined
avg vs $1, ladder state, confluence), fills tape, equity curve, resolved-cycle history
with CSV export, pause/halt controls.

Everything the bot sees and does is recorded to `data/YYYY-MM-DD/events.jsonl` —
paper runs double as backtest data collection.

## Trade log & analysis

Trading events (signals, vetoes, orders, fills, cancels, resolutions, halts — no feed
noise) are additionally appended to **`data/trades.jsonl`**, one JSON object per line.
That file is the analysis log:

- the dashboard **restores from it on startup** — fills tape, resolved-cycle history,
  equity curve, cumulative P&L, and today's daily P&L (so the daily-loss kill switch
  stays honest across restarts);
- `/api/trades.csv` exports every fill ever recorded; `/api/history.csv` exports the
  resolved cycles (both linked from the dashboard);
- being JSONL, it loads straight into pandas:
  `pd.read_json("data/trades.jsonl", lines=True)`.

## Backtest

After a few days of paper running:

```bat
.venv\Scripts\python -m bot.backtest.replay data\2026-09-01 data\2026-09-02
```

Reports the gate metrics from plan.md §3.7: P&L split by regime (choppy vs trending)
and by layer, achieved combined average cost, fill-rate calibration, expectancy,
drawdown, and the ROI arithmetic. **Tune only on the first half of your data; judge on
the untouched second half.**

## Tests

```bat
.venv\Scripts\python -m pytest tests\ -q
```

## Live mode (only after the plan.md §8 gates pass)

```bat
.venv\Scripts\pip install -e .[live]
```

(installs `py-clob-client-v2`, Polymarket's current official client — the original
`py-clob-client` builds an outdated order format and gets rejected with
"invalid order version")

Set in `.env`: `MODE=live`, `POLYGON_WALLET_PRIVATE_KEY`, and — for a normal Polymarket
account (funds deposited via the website) — `POLYMARKET_FUNDER_ADDRESS`, your deposit
address shown in the Polymarket UI. `POLYMARKET_SIGNATURE_TYPE=1` for accounts created
with email/Magic login (the default), `2` for accounts connected via a browser wallet
(MetaMask etc.). Leave the funder empty only if you trade from a raw EOA wallet that
holds its own USDC. API creds are derived from the key on first run (L2 auth). Orders
go through the Polymarket CLOB v2 API; fills stream from the user channel.

Not yet automated: on-chain redemption of resolved winnings — redeem via the
Polymarket UI (the bot logs a reminder at each resolution).

## Notes

- The strike is captured from Binance spot at window open, and windows joined late are
  skipped (no reliable open price).
- Resolution is settled against Binance spot as an **oracle proxy** in paper mode; the
  real oracle can disagree (see plan.md Known Risks).
- The spot feed host is `BINANCE_WS` in `.env`.
- Kill switch: daily loss beyond `MAX_DAILY_LOSS_USDC` cancels everything and halts;
  feed loss while exposed cancels all resting orders immediately.
