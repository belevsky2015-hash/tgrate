from __future__ import annotations

import asyncio
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import wallet as W
from .calibration import Calibrator
from .config import settings
from .graph import bind_rates, k_best_routes, load_graph, reachable_nodes
from .models import Unreachable
from .quotes import QuoteBook

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

app = FastAPI(title="Курс через Telegram Wallet")

book = QuoteBook(
    settings.wallet_api_key,
    side_when_we_buy=settings.wallet_side_buy,
    min_execute_rate=settings.wallet_min_execute_rate,
    merchants_only=settings.wallet_merchants_only,
    payments=settings.wallet_payments,
)
calib = Calibrator(settings.db_path)

VARS = {
    "exchange_fee": W.exchange_fee(settings.wallet_tier),
    "p2p_fee": W.p2p_fee(settings.wallet_role),
}
NODES, EDGES = load_graph(settings.fees_path, VARS)


def auth(x_token: str = Header(default="")):
    if settings.panel_token and x_token != settings.panel_token:
        raise HTTPException(401, "Неверный токен доступа")


class QuoteRequest(BaseModel):
    src: str
    dst: str
    amount: float = Field(gt=0)
    routes: int = 4


@app.on_event("startup")
async def warm():
    asyncio.create_task(_loop())


async def _loop():
    while True:
        try:
            await book.refresh(depth_usdt=1000)
        except Exception as exc:
            log.warning("Обновление котировок не удалось: %s", exc)
        await asyncio.sleep(30)   # P2P API Wallet обновляется раз в 30 сек


async def _ensure_fresh(depth_usdt: float):
    if not book.updated_at or time.time() - book.updated_at > 35:
        await book.refresh(depth_usdt=depth_usdt)


def _usd_depth(src: str, dst: str, amount: float) -> float:
    """Оценка размера сделки в долларах, чтобы взять стакан нужной глубины."""
    asset = NODES.get(src, {}).get("asset", "USDT")
    if asset in ("USDT", "USDC"):
        return amount
    ref = book.last.get(f"wallet:USDT/{asset}:bid") or book.last.get(
        f"fiat:{asset}/RUB:mid"
    )
    if asset == "RUB":
        rub = book.last.get("wallet:USDT/RUB:bid")
        return amount / rub if rub else amount / 100
    return amount / ref if ref else amount


@app.get("/api/nodes", dependencies=[Depends(auth)])
def nodes():
    live = reachable_nodes(EDGES)
    return [
        {"id": k, "title": v.get("title", k), "asset": v.get("asset")}
        for k, v in NODES.items() if k in live
    ]


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    return {
        "quotes": book.status(),
        "tier": settings.wallet_tier,
        "role": settings.wallet_role,
        "exchange_fee_pct": VARS["exchange_fee"],
        "p2p_fee_pct": VARS["p2p_fee"],
        "accuracy": calib.accuracy(),
        "wallet_vs_market": book.wallet_vs_market(),
    }


@app.post("/api/quote", dependencies=[Depends(auth)])
async def quote(req: QuoteRequest):
    await _ensure_fresh(_usd_depth(req.src, req.dst, req.amount))

    missing = bind_rates(EDGES, book.last)
    try:
        routes = k_best_routes(EDGES, req.src, req.dst, req.amount, k=req.routes)
    except Unreachable as exc:
        raise HTTPException(400, str(exc))
    if not routes:
        raise HTTPException(400, f"Маршрут {req.src} → {req.dst} не найден")

    pair = f"{req.src}>{req.dst}"
    out = []
    for r in routes:
        factor, basis, n = calib.factor(r.key, pair)
        adjusted = r.amount_out * factor * (1 - settings.safety_pct / 100)
        out.append({
            "key": r.key,
            "amount_out": round(adjusted, 8),
            "amount_out_raw": round(r.amount_out, 8),
            "effective_rate": adjusted / r.amount_in,
            "total_loss_pct": round((1 - adjusted / (r.amount_in * _mid(req, r))) * 100, 2)
                              if _mid(req, r) else None,
            "eta_min": r.eta_min,
            "calibration": {"factor": round(factor, 4), "basis": basis, "deals": n},
            "steps": [{
                "id": s.edge.id,
                "from": s.edge.src,
                "to": s.edge.dst,
                "kind": s.edge.kind,
                "in": round(s.amount_in, 8),
                "out": round(s.amount_out, 8),
                "rate": s.edge.rate,
                "rate_origin": s.edge.rate_origin,
                "loss_pct": round(s.loss_pct, 3),
                "fees": {k: round(v, 8) for k, v in s.fee_breakdown.items()},
                "note": s.edge.note,
            } for s in r.steps],
        })

    best = out[0]
    return {
        "request": req.model_dump(),
        "best": best,
        "alternatives": out[1:],
        "wallet_depth": book.wallet_detail,
        "missing_quotes": sorted(set(missing)),
        "quotes_age_sec": round(time.time() - book.updated_at, 1),
    }


def _mid(req: QuoteRequest, route) -> float | None:
    """Курс без комиссий — чтобы показать, сколько всего съела схема."""
    a = NODES.get(req.src, {}).get("asset")
    b = NODES.get(req.dst, {}).get("asset")
    if a == b:
        return 1.0
    if a in ("USDT", "USDC"):
        r = book.last.get(f"wallet:USDT/{b}:bid")
        return r
    if b in ("USDT", "USDC"):
        r = book.last.get(f"wallet:USDT/{a}:bid")
        return 1 / r if r else None
    ra = book.last.get(f"wallet:USDT/{a}:bid")
    rb = book.last.get(f"wallet:USDT/{b}:bid")
    return rb / ra if ra and rb else None


class DealIn(BaseModel):
    route_key: str
    pair: str
    amount_in: float
    predicted_out: float
    actual_out: float
    comment: str = ""


@app.post("/api/deals", dependencies=[Depends(auth)])
def add_deal(d: DealIn):
    did = calib.add_deal(d.route_key, d.pair, d.amount_in,
                         d.predicted_out, d.actual_out, d.comment)
    err = (d.actual_out / d.predicted_out - 1) * 100 if d.predicted_out else 0
    return {"id": did, "error_pct": round(err, 2), "accuracy": calib.accuracy(d.pair)}


@app.get("/api/deals", dependencies=[Depends(auth)])
def list_deals(limit: int = 30):
    return {"deals": calib.recent(limit), "accuracy": calib.accuracy()}


app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


@app.get("/")
def index():
    return FileResponse(f"{settings.static_dir}/index.html")
