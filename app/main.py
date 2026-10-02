from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import wallet as W
from .calibration import Calibrator
from .config import settings
from .graph import bind_rates, k_best_routes, load_graph, reachable_nodes
from .history import History
from .models import Unreachable
from .quotes import FIATS, QuoteBook

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
hist = History(settings.db_path)

VARS = W.fee_vars(settings.wallet_tier, settings.wallet_role)
NODES, EDGES = load_graph(settings.fees_path, VARS)


def auth(x_token: str = Header(default="")):
    if settings.panel_token and x_token != settings.panel_token:
        raise HTTPException(401, "Неверный токен доступа")


class QuoteRequest(BaseModel):
    src: str
    dst: str
    amount: float = Field(gt=0)
    routes: int = 4
    # Способы оплаты по фиату: {"EUR": ["sepainstant"]}. Нет фиата — из .env.
    pay: dict[str, list[str]] = {}


def _pay_names(fiat: str, pay: dict[str, list[str]]) -> list[str]:
    codes = pay.get(fiat) or (book.wallet._payments_for(fiat) if book.wallet else [])
    return [W.PAYMENT_NAMES.get(c, c) for c in codes]


def _custom_pay(pay: dict[str, list[str]]) -> bool:
    """Выбраны способы, отличные от настроек — для них истории нет."""
    if not book.wallet:
        return False
    return any(set(v) != set(book.wallet._payments_for(k)) for k, v in pay.items() if v)


@app.on_event("startup")
async def warm():
    asyncio.create_task(_loop())


async def _loop():
    while True:
        try:
            await book.refresh(depth_usdt=1000)
            hist.record(book.last)
            await _track()
        except Exception as exc:
            log.warning("Обновление котировок не удалось: %s", exc)
        await asyncio.sleep(30)   # P2P API Wallet обновляется раз в 30 сек


def _leg_prices(route) -> dict[str, float]:
    """Цена крипты на каждом шаге с котировкой — в валюте котировки за 1
    единицу: RUB за USDT, EUR за USDT, USDT за GRAM. Ключ — id ребра."""
    out = {}
    for s in route.steps:
        ref = s.edge.rate_ref or ""
        if not ref or not s.edge.rate:
            continue
        # ask и *_inv хранятся перевёрнутыми: сколько крипты за 1 единицу
        inv = ref.endswith(":ask") or ref.endswith("_inv")
        out[s.edge.id] = 1 / s.edge.rate if inv else s.edge.rate
    return out


def _legs_meta(edge_ids) -> list[dict]:
    """Подписи рядов по шагам: что за цена и какая её сторона нам выгодна."""
    by_id = {e.id: e for e in EDGES}
    out = []
    for eid in edge_ids:
        e = by_id.get(eid)
        if not e or not e.rate_ref:
            continue
        if e.rate_ref.startswith("wallet:"):
            fiat, key = e.rate_ref.split("/")[1].split(":")
            buy = key == "ask"
            title = f"USDT за {fiat} — {'покупаете' if buy else 'продаёте'}"
            unit = fiat
        else:                                   # cex:GRAMUSDT:bid
            sym, key = e.rate_ref.split(":")[1:3]
            buy = key.startswith("ask")
            title = f"{sym[:-4]} в USDT — {'покупаете' if buy else 'продаёте'} (биржа)"
            unit = "USDT"
        out.append({"id": eid, "title": title, "unit": unit,
                    "better": "low" if buy else "high"})
    return out


def _track_id(src: str, dst: str, amount: float) -> str:
    return f"{src}>{dst}:{amount:g}"


async def _track():
    """Точный итог маршрутов из TRACK_ROUTES — так же, как считает /api/quote.
    Объявления только что закешированы циклом, запросов к API не добавляет."""
    for src, dst, amount in settings.track_routes:
        quotes, _ = await book.at_depth(_usd_depth(src, dst, amount))
        bind_rates(EDGES, quotes)          # до k_best_routes нет await
        try:
            routes = k_best_routes(EDGES, src, dst, amount, k=1)
        except Unreachable:
            continue
        if routes:
            hist.record_tracked(_track_id(src, dst, amount),
                                routes[0].amount_out, routes[0].key,
                                _leg_prices(routes[0]))


async def _ensure_fresh(depth_usdt: float):
    if not book.updated_at or time.time() - book.updated_at > 35:
        await book.refresh(depth_usdt=depth_usdt)


def _usd_depth(src: str, dst: str, amount: float) -> float:
    """Размер сделки в USDT, чтобы взять стакан нужной глубины."""
    asset = NODES.get(src, {}).get("asset", "USDT")
    if asset in ("USDT", "USDC"):
        return amount
    spot = book.last.get(f"cex:{asset}USDT:bid")
    if spot:                                   # GRAM, BTC — через спот
        return amount * spot
    # фиат: сколько USDT купим на него по стакану цикла (ask хранится как
    # USDT за 1 фиат)
    ask = book.last.get(f"wallet:USDT/{asset}:ask")
    if ask:
        return amount * ask
    bid = book.last.get(f"wallet:USDT/{asset}:bid")
    return amount / bid if bid else amount


@app.get("/api/nodes", dependencies=[Depends(auth)])
def nodes():
    live = reachable_nodes(EDGES)
    # «Откуда» — точки, из которых есть операции, «Куда» — в которые есть.
    # USDT у контакта в Telegram — только точка назначения.
    srcs = {e.src for e in EDGES}
    dsts = {e.dst for e in EDGES}
    return [
        {"id": k, "title": v.get("title", k), "asset": v.get("asset"),
         "from": k in srcs, "to": k in dsts}
        for k, v in NODES.items() if k in live
    ]


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    return {
        "quotes": book.status(),
        "tier": settings.wallet_tier,
        "role": settings.wallet_role,
        "exchange_fee_pct": VARS["exchange_fee"],
        "p2p_fee_pct": VARS["p2p_fee_rub"],   # владелец торгует в основном рублём
        "accuracy": calib.accuracy(),
        "wallet_vs_market": book.wallet_vs_market(),
    }


@app.post("/api/quote", dependencies=[Depends(auth)])
async def quote(req: QuoteRequest):
    if req.src == req.dst:
        raise HTTPException(400, "Откуда и куда совпадают — считать нечего")
    await _ensure_fresh(1000)
    # Стакан Wallet пересчитывается на вашу сумму, а не на 1000 USDT цикла
    quotes, depth = await book.at_depth(_usd_depth(req.src, req.dst, req.amount),
                                        req.pay or None)

    # bind_rates правит общие EDGES; до k_best_routes ниже нет await,
    # поэтому параллельный запрос не подменит курсы посреди расчёта
    missing = _relevant_missing(req.src, req.dst, bind_rates(EDGES, quotes))
    try:
        routes = k_best_routes(EDGES, req.src, req.dst, req.amount, k=req.routes)
    except Unreachable as exc:
        raise HTTPException(400, str(exc))
    if not routes:
        title = lambda n: NODES.get(n, {}).get("title", n)
        why = (" На эту сумму нет цены: стакан не закрывает объём или по "
               "выбранному способу оплаты нет живых объявлений."
               if any(m.startswith("wallet:") for m in missing) else "")
        raise HTTPException(
            400, f"Маршрута «{title(req.src)}» → «{title(req.dst)}» нет.{why}")

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
        "wallet_depth": _legs(routes[0], depth, req.pay),
        "missing_quotes": sorted(set(missing)),
        "missing_steps": _missing_steps(missing),
        "quotes_age_sec": round(time.time() - book.updated_at, 1),
    }


def _relevant_missing(src: str, dst: str, missing: list[str]) -> list[str]:
    """Только котировки рёбер, через которые мог пройти маршрут src → dst:
    лира без цены не касается расчёта рубль → евро."""
    fwd, back = {src}, {dst}
    for _ in range(len(NODES)):
        fwd |= {e.dst for e in EDGES if e.src in fwd}
        back |= {e.src for e in EDGES if e.dst in back}
    refs = {e.rate_ref for e in EDGES if e.src in fwd and e.dst in back}
    return [m for m in missing if m in refs]


def _missing_steps(missing: list[str]) -> list[str]:
    title = lambda n: NODES.get(n, {}).get("title", n)
    return sorted({f"{title(e.src)} → {title(e.dst)}"
                   for e in EDGES if e.rate_ref in set(missing)})


def _legs(route, depth: dict, pay: dict) -> list[dict]:
    """Разбор стакана и топ продавцов только по P2P-ногам лучшего маршрута."""
    out = []
    for s in route.steps:
        ref = s.edge.rate_ref or ""
        if not ref.startswith("wallet:USDT/"):
            continue
        fiat, key = ref.split("/")[1].split(":")
        d = depth.get(f"{fiat}:{'buy' if key == 'ask' else 'sell'}")
        if d:
            out.append({"edge": s.edge.id, "fiat": fiat,
                        "direction": "buy" if key == "ask" else "sell",
                        "pay": _pay_names(fiat, pay), **d})
    return out


class DealIn(BaseModel):
    route_key: str
    pair: str
    amount_in: float = Field(gt=0)
    predicted_out: float = Field(gt=0)   # что было показано, с поправкой
    actual_out: float = Field(gt=0)
    model_out: float | None = None       # прогноз модели без поправки
    comment: str = ""


@app.get("/api/methods", dependencies=[Depends(auth)])
async def methods(fiat: str, direction: str):
    """Способы оплаты фиата из живого стакана. direction: buy — платим фиатом
    за USDT, sell — получаем фиат за USDT."""
    fiat = fiat.upper()
    if not book.wallet or fiat not in FIATS or direction not in ("buy", "sell"):
        raise HTTPException(400, "Нет стакана Wallet для этой валюты")
    async with httpx.AsyncClient() as c:
        rows = await book.wallet.methods(c, fiat, direction)
    return {"fiat": fiat, "direction": direction,
            "default": _pay_names(fiat, {}), "methods": rows}


@app.get("/api/history", dependencies=[Depends(auth)])
def history(src: str, dst: str, amount: float = 1000.0, hours: float = 24,
            pay: str = ""):
    """
    Курс направления по сохранённым срезам котировок.

    Граф собирается свой на каждый запрос: EDGES общий на процесс, а
    bind_rates его мутирует — привязка старых котировок затёрла бы
    текущие прямо под носом у параллельного /api/quote.
    """
    chosen = json.loads(pay) if pay else {}
    if _custom_pay(chosen):
        return {
            "src": src, "dst": dst, "amount": amount, "hours": hours,
            "points": [], "change_pct": None,
            "note": "история копится только для способов оплаты из настроек — "
                    "для выбранных её нет, а подставлять чужую нечестно",
        }

    tid = _track_id(src, dst, amount)
    if any(_track_id(*t) == tid for t in settings.track_routes):
        rows = hist.tracked(tid, hours)
        points = [{"t": round(ts), "rate": out / amount, "route": key, "legs": legs}
                  for ts, out, key, legs in rows]
        first, last_p = (points[0]["rate"], points[-1]["rate"]) if points else (None, None)
        return {
            "src": src, "dst": dst, "amount": amount, "hours": hours,
            "points": points,
            "change_pct": round((last_p / first - 1) * 100, 4) if points and first else None,
            "legs_meta": _legs_meta(dict.fromkeys(k for p in points for k in p["legs"])),
            "note": f"точный расчёт на {amount:g} раз в минуту — цены надёжных "
                    f"продавцов на эту сумму, без поправки калибровки",
        }

    _, edges = load_graph(settings.fees_path, VARS)
    points = []
    for ts, rates in hist.snapshots(hours):
        bind_rates(edges, rates)
        try:
            routes = k_best_routes(edges, src, dst, amount, k=1)
        except Unreachable:
            continue
        if routes:
            points.append({"t": round(ts), "rate": routes[0].amount_out / amount,
                           "route": routes[0].key, "legs": _leg_prices(routes[0])})

    first, last_p = (points[0]["rate"], points[-1]["rate"]) if points else (None, None)
    return {
        "src": src, "dst": dst, "amount": amount, "hours": hours,
        "points": points,
        "change_pct": round((last_p / first - 1) * 100, 4) if points and first else None,
        "legs_meta": _legs_meta(dict.fromkeys(k for p in points for k in p["legs"])),
        "note": "цены срезов — стакан на 1000 USDT; на другой сумме цена "
                "другая. Для точного графика добавьте маршрут в TRACK_ROUTES",
    }


@app.post("/api/deals", dependencies=[Depends(auth)])
def add_deal(d: DealIn):
    # В базу — прогноз модели: от него калибровка и считает поправку. Панель
    # присылает его сама; юзербот знает только показанное число, тогда
    # снимаем с него текущую поправку и запас.
    model = d.model_out
    if not model:
        factor, _, _ = calib.factor(d.route_key, d.pair)
        model = d.predicted_out / (factor * (1 - settings.safety_pct / 100))
    did = calib.add_deal(d.route_key, d.pair, d.amount_in,
                         model, d.actual_out, d.comment)
    err = (d.actual_out / d.predicted_out - 1) * 100
    return {"id": did, "error_pct": round(err, 2), "accuracy": calib.accuracy(d.pair)}


@app.get("/api/deals", dependencies=[Depends(auth)])
def list_deals(limit: int = 30):
    return {"deals": calib.recent(limit), "accuracy": calib.accuracy()}


app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


@app.get("/")
def index():
    return FileResponse(f"{settings.static_dir}/index.html")
