"""Тесты движка. Сеть не нужна, котировки подставляются вручную."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import wallet as W
from app.calibration import Calibrator
from app.graph import bind_rates, k_best_routes, load_graph
from app.models import Edge, Unreachable

FEES = str(Path(__file__).resolve().parent.parent / "fees.yaml")

QUOTES = {
    "wallet:USDT/RUB:ask": 1 / 78.5,
    "wallet:USDT/RUB:bid": 77.2,
    "wallet:USDT/KZT:ask": 1 / 525.0,
    "wallet:USDT/KZT:bid": 520.0,
    "cex:TONUSDT:bid": 2.10,
    "cex:TONUSDT:ask_inv": 1 / 2.12,
    "cex:BTCUSDT:bid": 95000.0,
    "cex:BTCUSDT:ask_inv": 1 / 95100.0,
    "fiat:USD/RUB:bid": 79.0,
    "fiat:USD/RUB:ask": 1 / 81.0,
}


def graph(tier="basic", role="taker"):
    nodes, edges = load_graph(FEES, {
        "exchange_fee": W.exchange_fee(tier),
        "p2p_fee": W.p2p_fee(role),
    })
    bind_rates(edges, QUOTES)
    return nodes, edges


# --- комиссии --------------------------------------------------------------

def test_taker_ne_platit_a_maker_platit():
    assert W.p2p_fee("taker") == 0.0
    assert W.p2p_fee("maker") == 0.9


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
    assert (1 - b.amount_out / a.amount_out) * 100 == pytest.approx(0.9, abs=0.05)


def test_treider_vygodnee_na_obmene():
    _, basic = graph(tier="basic")
    _, trader = graph(tier="trader")
    a = k_best_routes(basic, "USDT.WALLET", "GRAM.CHAIN", 5000, k=1)[0]
    b = k_best_routes(trader, "USDT.WALLET", "GRAM.CHAIN", 5000, k=1)[0]
    assert b.amount_out > a.amount_out


def test_p2p_luchshe_karty():
    _, edges = graph()
    routes = k_best_routes(edges, "USDT.WALLET", "RUB.BANK", 1000, k=4)
    best = routes[0]
    assert "card.sell" not in best.key, "Карта с 3,5% не может быть лучшим маршрутом"


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
    for _ in range(5):
        c.add_deal("r1", "RUB>USDT", 10000, 100.0, 97.0)
    f, basis, n = c.factor("r1", "RUB>USDT")
    assert f == pytest.approx(0.97, abs=0.005)
    assert basis == "маршрут"


def test_odna_sdelka_ne_menyaet_popravku(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    c.add_deal("r1", "RUB>USDT", 10000, 100.0, 90.0)
    assert c.factor("r1", "RUB>USDT")[0] == 1.0


def test_vybros_ne_utaskivaet_popravku(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    for _ in range(6):
        c.add_deal("r1", "RUB>USDT", 10000, 100.0, 99.0)
    c.add_deal("r1", "RUB>USDT", 10000, 100.0, 10.0)   # опечатка
    f, _, _ = c.factor("r1", "RUB>USDT")
    assert 0.97 < f < 1.0


def test_popravka_ogranichena_sverhu(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    for _ in range(5):
        c.add_deal("r1", "RUB>USDT", 10000, 100.0, 180.0)
    assert c.factor("r1", "RUB>USDT")[0] <= 1.08


def test_padaet_na_napravlenie_esli_marshrut_novyi(tmp_path):
    c = Calibrator(str(tmp_path / "d.sqlite"))
    for _ in range(4):
        c.add_deal("staryi", "RUB>USDT", 10000, 100.0, 98.0)
    f, basis, _ = c.factor("novyi", "RUB>USDT")
    assert basis == "направление"
    assert f == pytest.approx(0.98, abs=0.01)
