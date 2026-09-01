"""Local web dashboard: FastAPI + one self-contained page + websocket state feed."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse

log = logging.getLogger("dashboard")

STATIC = Path(__file__).parent / "static" / "index.html"


def build_app(hub, spot_feed, executor, risk) -> FastAPI:
    app = FastAPI(title="polymarket-momentum-bot")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return STATIC.read_text(encoding="utf-8")

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        try:
            while True:
                await sock.send_json(hub.snapshot(spots=spot_feed.snapshot()))
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.post("/api/control/{action}")
    async def control(action: str) -> dict:
        if action == "pause":
            hub.paused = True
            hub.note("entries PAUSED (dashboard)")
        elif action == "resume":
            hub.paused = False
            hub.note("entries resumed (dashboard)")
        elif action == "halt":
            risk.halt("manual flatten-and-halt (dashboard)")
        else:
            return {"ok": False, "error": "unknown action"}
        return {"ok": True, "paused": hub.paused, "halted": hub.halted}

    @app.get("/api/history.csv", response_class=PlainTextResponse)
    async def history_csv() -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["resolved_ts", "question", "asset", "duration_s", "winner", "pnl",
                    "layer1_pnl", "layer2_pnl", "combined_avg", "matched", "skew_side",
                    "skew_shares", "regime", "fills"])
        for h in hub.history:
            w.writerow([h.resolved_ts, h.market.question, h.market.asset, h.market.duration_s,
                        h.winner.value, round(h.pnl, 4), round(h.layer_pnl.get(1, 0), 4),
                        round(h.layer_pnl.get(2, 0), 4),
                        round(h.combined_avg, 4) if h.combined_avg else "",
                        h.matched, h.skew_side or "", h.skew_shares, h.regime, h.fills])
        return buf.getvalue()

    return app


async def serve(app: FastAPI, host: str, port: int) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    log.info("dashboard at http://%s:%s", host, port)
    await server.serve()
