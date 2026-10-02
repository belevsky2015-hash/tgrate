"""Тесты движка. Сеть не нужна, котировки подставляются вручную."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import wallet as W
from app.calibration import Calibrator
from app.history import History
from app.graph import bind_rates, k_best_routes, load_graph
from app.models import Edge, Unreachable

FEES = str(Path(__file__).resolve().parent.parent / "fees.yaml")

QUOTES = {
    "wallet:USDT/RUB:ask": 1 / 78.5,
    "wallet:USDT/RUB:bid": 77.2,
    "wallet:USDT/KZT:ask": 1 / 525.0,
    "wallet:USDT/KZT:bid": 520.0,
    "cex:GRAMUSDT:bid": 2.10,
    "cex:GRAMUSDT:ask_inv": 1 / 2.12,
    "cex:BTCUSDT:bid": 95000.0,
    "cex:BTCUSDT:ask_inv": 1 / 95100.0,
}


def graph(tier="basic", role="taker"):
    nodes, edges = load_graph(FEES, W.fee_vars(tier, role))
    bind_rates(edges, QUOTES)
    return nodes, edges


# --- комиссии --------------------------------------------------------------

def test_taker_ne_platit_a_maker_platit():
    # тариф P2P с 10.08.2026: рубль дороже остальных валют
    assert W.p2p_fee("taker", "RUB") == 0.0
    assert W.p2p_fee("maker", "RUB") == 2.0
    assert W.p2p_fee("maker", "KZT") == 1.2


def test_tarif_treider_deshevle_v_desyat_raz():
    assert W.exchange_fee("basic") / W.exchange_fee("trader") == pytest.approx(10, rel=.01)


def test_fiksirovannyi_vyvod_ne_zavisit_ot_summy():
    e = Edge(id="t", src="a", dst="b", rate_const=1.0, fee_dst=3.5)
    e.rate = 1.0
    assert e.apply(100).amount_out == pytest.approx(96.5)
    assert e.apply(10000).amount_out == pytest.approx(9996.5)


def test_vyvod_dorozhe_summy_ne_prohodit():
    e = Edge(id="t", src="a", dst="b", rate_const=1.0, fee_dst=3.5)
    e.rate = 1.0
    with pytest.raises(Unreachable):
        e.apply(2)


def test_procent_i_fiks_skladyvayutsya_v_pravilnom_poryadke():
    # процент берётся с результата обмена, фикс сети вычитается после
    e = Edge(id="t", src="a", dst="b", rate_const=2.0, fee_pct=1.0, fee_dst=5.0)
    e.rate = 2.0
    r = e.apply(100)
    assert r.amount_out == pytest.approx(100 * 2 * 0.99 - 5)


# --- стакан P2P ------------------------------------------------------------

def ad(price, volume, lo, hi, er=1.0, orders=500, level="MERCHANT", nick="x"):
    return W.Ad(price=price, volume=volume, min_fiat=lo, max_fiat=hi,
                execute_rate=er, merchant_level=level, is_online=True,
                auto_accept=False, payments=[], nickname=nick, order_num=orders)


def test_obyavlenie_s_minimumom_vyshe_summy_ne_beretsya():
    # Живой случай 27.09.2026: 91 ₽ только ровно на 100 000 ₽. На 1000 USDT
    # (91 000 ₽) эту сделку не открыть — курс должен быть 90, а не 91.
    pool = [ad(91.0, 19000, 100_000, 100_000), ad(90.0, 5000, 1000, 500_000)]
    cost, got, used = W.fill(pool, 1000)
    assert got == pytest.approx(1000)
    assert cost / got == pytest.approx(90.0)


def test_maksimum_obyavleniya_rezhet_kusok():
    pool = [ad(91.0, 10_000, 1000, 45_500), ad(90.0, 10_000, 1000, 500_000)]
    cost, got, _ = W.fill(pool, 1000)
    # 500 USDT по 91 (упёрлись в максимум), остальное по 90
    assert cost / got == pytest.approx(90.5)


def test_top_tolko_nadezhnye_i_na_vsyu_summu():
    pool = [
        ad(92.0, 5000, 1000, 500_000, er=0.80, nick="ненадёжный"),
        ad(91.5, 300, 1000, 500_000, nick="мало объёма"),
        ad(91.0, 5000, 1000, 500_000, orders=40, er=0.99, nick="мало сделок"),
        ad(90.8, 5000, 1000, 500_000, orders=40, level="TRUSTED_MERCHANT", nick="надёжный"),
        ad(90.5, 5000, 1000, 500_000, nick="a"),
        ad(90.0, 5000, 1000, 500_000, nick="b"),
        ad(89.0, 5000, 1000, 500_000, nick="c"),
    ]
    top = W.top_offers(pool, 1000)
    assert [t["nickname"] for t in top] == ["надёжный", "a", "b"]


def test_prognoz_iz_teh_zhe_prodavcov_chto_v_tope():
    import asyncio
    w = W.WalletP2P("k", min_execute_rate=0.9)
    book = [
        ad(92.0, 50, 100, 5000, er=0.91, orders=10, nick="мелкий непроверенный"),
        ad(91.0, 5000, 1000, 500_000, nick="надёжный"),
        ad(80.0, 5000, 1000, 500_000, nick="дешёвый"),
    ]

    async def fake_ads(*_):
        return book
    w.ads = fake_ads
    q = asyncio.run(w.quote(None, "USDT", "RUB", "sell", 100))
    # продаём 100 USDT: мелкого непроверенного не дробим, берём надёжного
    assert q["price"] == pytest.approx(91.0)
    assert q["top"][0]["nickname"] == "надёжный"
    # 6 000 USDT одной сделкой не закрыть — VWAP по надёжным: 5000 по 91 и
    # 1000 по 80
    q = asyncio.run(w.quote(None, "USDT", "RUB", "sell", 6_000))
    assert q["top"] == [] and q["basis"] == "несколько сделок у надёжных"
    assert q["price"] == pytest.approx((5000 * 91 + 1000 * 80) / 6000)


def test_nedobor_obema_ne_dorisovyvaetsya():
    import asyncio
    w = W.WalletP2P("k", min_execute_rate=0.9)

    async def fake_ads(*_):
        return [ad(90.0, 100, 1000, 500_000)]
    w.ads = fake_ads
    # в стакане 100 USDT, просим 1000 — котировки нет, а не «90 плюс 1%»
    assert asyncio.run(w.quote(None, "USDT", "RUB", "buy", 1000)) is None


def test_sposob_iz_zaprosa_meniaet_prodavcov():
    import asyncio
    w = W.WalletP2P("k", min_execute_rate=0.9, payments={"EUR": ["sepainstant"]})
    a1 = ad(0.85, 5000, 30, 1500, nick="instant")
    a1.payments = ["sepainstant", "revolut"]
    a2 = ad(0.842, 5000, 100, 11000, nick="sepa")
    a2.payments = ["sepaeu"]

    async def fake_ads(*_):
        return [a1, a2]
    w.ads = fake_ads
    q = asyncio.run(w.quote(None, "USDT", "EUR", "sell", 200))
    assert q["price"] == pytest.approx(0.85)                    # из настроек
    q = asyncio.run(w.quote(None, "USDT", "EUR", "sell", 200, ["sepaeu"]))
    assert q["price"] == pytest.approx(0.842)                   # выбран SEPA
    m = asyncio.run(w.methods(None, "EUR", "sell"))
    assert [x["name"] for x in m][:1] and {x["code"] for x in m} == {"sepainstant", "revolut", "sepaeu"}


# --- маршруты --------------------------------------------------------------

def test_na_maloi_summe_vygodnee_set_ton():
    _, edges = graph()
    r = k_best_routes(edges, "RUB.BANK", "USDT.WALLET", 8000, k=1)[0]
    ton = k_best_routes(edges, "RUB.BANK", "USDT.TON", 8000, k=1)[0]
    trc = k_best_routes(edges, "RUB.BANK", "USDT.TRC20", 8000, k=1)[0]
    assert ton.amount_out > trc.amount_out
    # 3,5 USDT против 1 USDT — разница ровно 2,5
    assert ton.amount_out - trc.amount_out == pytest.approx(2.5, abs=0.01)
    assert r.amount_out > ton.amount_out


def test_na_krupnoi_summe_raznica_setei_rastvoryaetsya():
    _, edges = graph()
    ton = k_best_routes(edges, "RUB.BANK", "USDT.TON", 4_000_000, k=1)[0]
    trc = k_best_routes(edges, "RUB.BANK", "USDT.TRC20", 4_000_000, k=1)[0]
    assert (ton.amount_out / trc.amount_out - 1) * 100 < 0.1


def test_maker_teryaet_bolshe_takera():
    _, taker = graph(role="taker")
    _, maker = graph(role="maker")
    a = k_best_routes(taker, "RUB.BANK", "USDT.WALLET", 100000, k=1)[0]
    b = k_best_routes(maker, "RUB.BANK", "USDT.WALLET", 100000, k=1)[0]
    assert a.amount_out > b.amount_out
    assert (1 - b.amount_out / a.amount_out) * 100 == pytest.approx(2.0, abs=0.05)


def test_treider_vygodnee_na_obmene():
    _, basic = graph(tier="basic")
    _, trader = graph(tier="trader")
    a = k_best_routes(basic, "USDT.WALLET", "GRAM.CHAIN", 5000, k=1)[0]
    b = k_best_routes(trader, "USDT.WALLET", "GRAM.CHAIN", 5000, k=1)[0]
    assert b.amount_out > a.amount_out


def test_marshrut_bez_kotirovok_ne_predlagaetsya():
    _, edges = graph()
    # UAH котировок в QUOTES нет — маршрута быть не должно
    assert k_best_routes(edges, "RUB.BANK", "UAH.BANK", 50000, k=1) == []


def test_v_marshrute_net_ciklov():
    _, edges = graph()
    for r in k_best_routes(edges, "RUB.BANK", "KZT.BANK", 50000, k=5):
        nodes = [s.edge.src for s in r.steps] + [r.steps[-1].edge.dst]
        assert len(nodes) == len(set(nodes))


def test_bolshe_zashlo_ne_menshe_vyshlo():
    """Инвариант, без которого поиск маршрута перестаёт быть корректным."""
    _, edges = graph()
    prev = 0
    for amount in (1000, 5000, 20000, 100000, 500000):
        r = k_best_routes(edges, "RUB.BANK", "USDT.TON", amount, k=1)
        if not r:
            continue
        assert r[0].amount_out > prev
        prev = r[0].amount_out


# --- калибровка ------------------------------------------------------------

def test_popravka_shoditsya_k_faktu(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    for i in range(5):              # разные сделки — разные суммы
        c.add_deal("r1", "RUB>USDT", 10000 + i, 100.0, 97.0)
    f, basis, n = c.factor("r1", "RUB>USDT")
    assert f == pytest.approx(0.97, abs=0.005)
    assert basis == "маршрут"


def test_odna_sdelka_ne_menyaet_popravku(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    c.add_deal("r1", "RUB>USDT", 10000, 100.0, 90.0)
    assert c.factor("r1", "RUB>USDT")[0] == 1.0


def test_vybros_ne_utaskivaet_popravku(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    for i in range(6):
        c.add_deal("r1", "RUB>USDT", 10000 + i, 100.0, 99.0)
    c.add_deal("r1", "RUB>USDT", 10000, 100.0, 10.0)   # опечатка
    f, _, _ = c.factor("r1", "RUB>USDT")
    assert 0.97 < f < 1.0


def test_dvoinoe_nazhatie_ne_dubliruet_sdelku(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    a = c.add_deal("r1", "RUB>EUR", 20000, 186.65, 186.5)
    b = c.add_deal("r1", "RUB>EUR", 20000, 186.65, 186.5)
    assert a == b and len(c.recent()) == 1


def test_popravka_ne_raskachivaetsya():
    """Поправка считается от прогноза модели: когда модель стабильно врёт на
    3%, поправка встаёт на 0,97 и остаётся там, а не тянется обратно к 1."""
    import tempfile, os
    from fastapi.testclient import TestClient
    import app.main as m
    m.calib = Calibrator(os.path.join(tempfile.mkdtemp(), "d.sqlite"))
    c = TestClient(m.app)
    raw = 100.0
    for i in range(8):
        f, _, _ = m.calib.factor("r1", "RUB>USDT")
        shown = raw * f                                  # что увидел владелец
        c.post("/api/deals", json={"route_key": "r1", "pair": "RUB>USDT",
               "amount_in": 10000 + i, "predicted_out": shown, "actual_out": 97.0})
    assert m.calib.factor("r1", "RUB>USDT")[0] == pytest.approx(0.97, abs=0.003)


def test_popravka_ogranichena_sverhu(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    for i in range(5):
        c.add_deal("r1", "RUB>USDT", 10000 + i, 100.0, 180.0)
    assert c.factor("r1", "RUB>USDT")[0] <= 1.08


def test_padaet_na_napravlenie_esli_marshrut_novyi(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    for i in range(4):
        c.add_deal("staryi", "RUB>USDT", 10000 + i, 100.0, 98.0)
    f, basis, _ = c.factor("novyi", "RUB>USDT")
    assert basis == "направление"
    assert f == pytest.approx(0.98, abs=0.01)


# --- история ---------------------------------------------------------------

def test_srezy_staroi_modeli_v_grafik_ne_idut(tmp_path):
    import json, sqlite3, time
    h = History(str(tmp_path / "d.sqlite"))
    with sqlite3.connect(h.path) as c:     # срез до MODEL = 2, без метки
        c.execute("INSERT INTO rate_snapshots VALUES(?, ?)",
                  (time.time() - 60, json.dumps({"wallet:USDT/EUR:bid": 0.865})))
    h.record({"wallet:USDT/EUR:bid": 0.85})
    assert [r["wallet:USDT/EUR:bid"] for _, r in h.snapshots(24)] == [0.85]


def test_otslezhivaemyi_marshrut_pishetsya_raz_v_minutu(tmp_path):
    h = History(str(tmp_path / "d.sqlite"))
    legs = {"p2p.buy.rub": 91.0, "p2p.sell.eur": 0.85}
    assert h.record_tracked("USDT.WALLET>RUB.BANK:1000", 186.81, "p2p.sell.rub", legs)
    assert not h.record_tracked("USDT.WALLET>RUB.BANK:1000", 186.90, "x")   # чаще минуты
    rows = h.tracked("USDT.WALLET>RUB.BANK:1000", 24)
    assert [(out, key, lg) for _, out, key, lg in rows] == [(186.81, "p2p.sell.rub", legs)]


def test_staraya_tablica_tracked_dopolnyaetsya(tmp_path):
    import sqlite3, time
    path = str(tmp_path / "d.sqlite")
    with sqlite3.connect(path) as c:        # как на проде до 27.09.2026 вечера
        c.execute("CREATE TABLE tracked (route TEXT NOT NULL, ts REAL NOT NULL, "
                  "amount_out REAL NOT NULL, route_key TEXT NOT NULL, PRIMARY KEY (route, ts))")
        c.execute("INSERT INTO tracked VALUES('r', ?, 186.61, 'k')", (time.time(),))
    h = History(path)
    h.record_tracked("r", 187.85, "k", {"p2p.buy.rub": 90.5})
    assert [(out, lg) for _, out, _, lg in h.tracked("r", 24)] == [(186.61, {}), (187.85, {"p2p.buy.rub": 90.5})]
