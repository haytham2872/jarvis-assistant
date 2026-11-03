"""Totals in the owner's base currency (JARVIS_BASE_CURRENCY): cross rates through the euro."""
import os
import pathlib
import subprocess
import sys
from decimal import Decimal

from backend.market import data

ROOT = pathlib.Path(__file__).resolve().parents[2]

ECB = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<Cube><Cube time="2026-10-30"><Cube currency="USD" rate="1.25"/><Cube currency="GBP" rate="0.8"/></Cube></Cube>
</gesmes:Envelope>"""


def _rates(rows):
    return {r["pair"]: Decimal(r["rate"]) for r in rows}


def test_the_euro_base_is_the_ecb_inverse():
    assert _rates(data.parse_ecb(ECB, "EUR")) == {"USD/EUR": Decimal("0.8"), "GBP/EUR": Decimal("1.25")}


def test_another_base_is_a_cross_rate_and_includes_the_euro():
    # 1 EUR = 1.25 USD = 0.8 GBP, so 1 GBP = 1.5625 USD and 1 EUR = 1.25 USD.
    assert _rates(data.parse_ecb(ECB, "USD")) == {"GBP/USD": Decimal("1.5625"), "EUR/USD": Decimal("1.25")}
    assert _rates(data.parse_ecb(ECB, "GBP")) == {"USD/GBP": Decimal("0.64"), "EUR/GBP": Decimal("0.8")}


def test_a_day_without_the_base_gives_no_rates():
    assert data.cross_rates({"USD": "1.25"}, "JPY") == {}


def test_history_is_crossed_by_date_and_needs_both_legs():
    usd = [{"day": "2026-10-29", "rate": "0.8"}, {"day": "2026-10-30", "rate": "0.8"}]   # EUR per USD
    gbp = [{"day": "2026-10-30", "rate": "1.25"}]                                       # EUR per GBP
    assert data.cross_history(usd, gbp) == [{"day": "2026-10-30", "rate": "0.64"}]
    assert data.cross_history(None, gbp) == [{"day": "2026-10-30", "rate": "0.8"}]     # EUR in GBP
    assert data.cross_history(usd, None) == usd                                         # euro base


def test_the_ledger_views_and_lookups_follow_the_base():
    code = ("from backend.market import data; from backend.storage import portfolio as p;"
            "print(data.BASE, \"'EUR'\" in p.SCHEMA, \"||'/USD'\" in p.SCHEMA, p.fx_rate('USD', '2026-10-30'))")
    env = dict(os.environ, JARVIS_BASE_CURRENCY="usd", PYTHONPATH=str(ROOT),
               JARVIS_DB_PATH=str(ROOT / ".pytest-base-currency.db"))
    try:
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                             timeout=120, cwd=str(ROOT / "backend"))
    finally:
        for suffix in ("", "-wal", "-shm"):
            (ROOT / f".pytest-base-currency.db{suffix}").unlink(missing_ok=True)
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.split()[:3] == ["USD", "False", "True"]
    assert "'rate': '1'" in out.stdout                      # the base itself needs no rate


def test_an_unusable_setting_falls_back_to_the_euro(monkeypatch):
    monkeypatch.setenv("JARVIS_BASE_CURRENCY", "dollars")
    assert data._base_currency() == "EUR"
