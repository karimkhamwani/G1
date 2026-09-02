"""Market discovery via the Gamma API. (Fees are not modeled — Polymarket settles
them on-chain; entry margins must cover them.)

Polymarket's short-duration crypto markets have deterministic slugs:
    {asset}-updown-{5m|15m}-{window_start_unix}     e.g. btc-updown-5m-1788289500
so discovery CONSTRUCTS the slug for the current and next window per asset/duration
and fetches each directly — no listing scans. The window open is `eventStartTime`
(`startDate` is market creation time, not the window!) and close is `endDate`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import aiohttp

from bot.models import Market, Side

log = logging.getLogger("discovery")

POLL_S = 10
UA = {"User-Agent": "Mozilla/5.0 (momentum-bot)"}
DUR_TAG = {300: "5m", 900: "15m", 3600: "1h"}


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Discovery:
    def __init__(self, settings, hub, recorder, on_new_market) -> None:
        self.s = settings
        self.hub = hub
        self.recorder = recorder
        self.on_new_market = on_new_market  # callback(Market)
        self._seen: set[str] = set()

    def _candidate_slugs(self, now: float) -> list[tuple[str, str, int, int]]:
        """(slug, asset, duration_s, window_start) for current + next window of each series."""
        out = []
        for asset in self.s.asset_list:
            for dur in self.s.duration_list_s:
                tag = DUR_TAG.get(dur)
                if not tag:
                    continue
                current = int(now) - int(now) % dur
                for start in (current, current + dur):
                    slug = f"{asset.lower()}-updown-{tag}-{start}"
                    out.append((slug, asset, dur, start))
        return out

    async def run(self) -> None:
        async with aiohttp.ClientSession(headers=UA) as http:
            while True:
                try:
                    await self._poll(http)
                    self.hub.feed_beat("discovery")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("discovery error: %s", e)
                await asyncio.sleep(POLL_S)

    async def _poll(self, http: aiohttp.ClientSession) -> None:
        now = time.time()
        for slug, asset, dur, win_start in self._candidate_slugs(now):
            if slug in self._seen:
                continue
            m = await self._fetch_market(http, slug, asset, dur)
            if m is None:
                continue
            if m.end_ts <= now:
                self._seen.add(slug)
                continue
            self._seen.add(slug)
            self.recorder.log("market_discovered", {
                "condition_id": m.condition_id, "slug": m.slug, "question": m.question,
                "asset": m.asset, "duration_s": m.duration_s,
                "start_ts": m.start_ts, "end_ts": m.end_ts,
                "token_yes": m.token[Side.YES], "token_no": m.token[Side.NO],
            })
            log.info("new market: %s (%s %ss, opens in %.0fs)",
                     slug, asset, dur, m.start_ts - now)
            self.on_new_market(m)

    async def _fetch_market(self, http, slug: str, asset: str, dur: int) -> Market | None:
        url = f"{self.s.gamma_host}/markets"
        try:
            async with http.get(url, params={"slug": slug},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                rows = await resp.json()
        except Exception as e:  # noqa: BLE001
            log.debug("fetch %s failed: %s", slug, e)
            return None
        if not rows:
            return None
        row = rows[0]
        # window open is eventStartTime; endDate is the close
        start = _parse_iso(row.get("eventStartTime")) or _parse_iso(row.get("gameStartTime"))
        end = _parse_iso(row.get("endDate"))
        cid = row.get("conditionId") or row.get("condition_id")
        raw_tokens = row.get("clobTokenIds")
        raw_outcomes = row.get("outcomes")
        if not (start and end and cid and raw_tokens):
            return None
        try:
            tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or [])
        except json.JSONDecodeError:
            return None
        if len(tokens) != 2:
            return None
        # map outcome labels: Up/Yes -> YES side
        yes_idx = 0
        for i, o in enumerate(outcomes[:2]):
            if str(o).strip().lower() in ("yes", "up"):
                yes_idx = i
        return Market(
            condition_id=cid, slug=slug, question=row.get("question") or slug,
            asset=asset, duration_s=dur, start_ts=start, end_ts=end,
            token={Side.YES: str(tokens[yes_idx]), Side.NO: str(tokens[1 - yes_idx])},
            # captured here so live order creation needs NO metadata fetch (zero-prereq
            # signing): tick size validates the price grid, neg_risk picks the contract
            # the EIP-712 signature is bound to
            tick_size=float(row.get("orderPriceMinTickSize") or 0.01),
            neg_risk=bool(row.get("negRisk")),
        )
