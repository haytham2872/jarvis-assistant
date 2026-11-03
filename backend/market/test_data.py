import asyncio
from datetime import datetime
from decimal import Decimal

import pytest

from backend.market import data


def instrument():
    return {"symbol":"NVDA","price_key":"NVDA","currency":"USD","exchange":"NASDAQ"}


def test_yahoo_does_not_use_dividend_adjusted_close_and_undoes_split():
    ts=datetime(2026,9,1,tzinfo=data.LOCAL_TZ).timestamp()
    body={"chart":{"result":[{"meta":{"symbol":"NVDA","currency":"USD","exchangeTimezoneName":"America/New_York"},
        "timestamp":[ts],"indicators":{"quote":[{"close":[50.0000001]}],"adjclose":[{"adjclose":[49]}]},
        "events":{"splits":{"1":{"date":ts+86400,"numerator":2,"denominator":1}}}}]}}
    assert data.parse_yahoo(body,instrument())[0]["close"]=="100.00"
    body["chart"]["result"][0]["meta"]["currency"]="EUR"
    with pytest.raises(ValueError): data.parse_yahoo(body,instrument())


@pytest.mark.parametrize("value", ["NaN","Infinity","-1","0","1e50","1e-100",None])
def test_invalid_feed_numbers_are_refused(value):
    with pytest.raises(ValueError): data.number(value)


def test_ecb_inverse_is_decimal_and_dated():
    xml='<Cube><Cube time="2026-09-01"><Cube currency="USD" rate="1.25"/></Cube></Cube>'
    row=data.parse_ecb(xml)[0]
    assert row["pair"]=="USD/EUR" and Decimal(row["rate"])==Decimal("0.8")
    assert row["day"]=="2026-09-01"


def test_requested_close_missing_uses_fallback(monkeypatch):
    import json
    calls=[]
    ts=datetime(2026,9,1,16,tzinfo=data.ZoneInfo("America/New_York")).timestamp()
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url):
            calls.append(url)
            body=({"data":{"tradesTable":{"rows":[]}}} if "nasdaq" in url else
                {"chart":{"result":[{"meta":{"symbol":"NVDA","currency":"USD","exchangeTimezoneName":"America/New_York"},
                    "timestamp":[ts],"indicators":{"quote":[{"close":[200]}]}}]}})
            return type("Response",(),{"text":json.dumps(body),"raise_for_status":lambda self:None})()
    monkeypatch.setattr(data.httpx,"AsyncClient",Client)
    monkeypatch.delenv("JARVIS_PORTFOLIO_SOURCES",raising=False)
    result=asyncio.run(data.get_closes(instrument(),"2026-09-01"))
    assert result["source"]=="yahoo" and result["day"]=="2026-09-01"
    assert len(calls)==2


def test_weekend_fx_preserves_actual_reference_dates_and_all_currencies(monkeypatch):
    calls=[]
    xml='<Cube><Cube time="2026-10-01"><Cube currency="USD" rate="1.25"/><Cube currency="GBP" rate="0.8"/></Cube><Cube time="2026-10-05"><Cube currency="USD" rate="1.1"/></Cube></Cube>'
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url,**kw):
            calls.append(url)
            return type('Response',(),{'text':xml,'raise_for_status':lambda self:None})()
    monkeypatch.setattr(data.httpx,'AsyncClient',Client)
    rows=asyncio.run(data.get_fx('2026-10-03'))
    assert len(calls)==1
    assert {r['pair'] for r in rows}=={'USD/EUR','GBP/EUR'}
    assert {r['day'] for r in rows}=={'2026-10-01'}
    assert Decimal(next(r['rate'] for r in rows if r['pair']=='GBP/EUR'))==Decimal('1.25')


def test_fx_fallback_refuses_out_of_window_rates(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url,**kw):
            calls.append(url)
            if len(calls)==1:
                raise ValueError('XML unavailable')
            csv='TIME_PERIOD,OBS_VALUE\n2026-09-01,1.25\n2026-10-05,1.1\n'
            return type('Response',(),{'text':csv,'url':url,'raise_for_status':lambda self:None})()
    monkeypatch.setattr(data.httpx,'AsyncClient',Client)
    with pytest.raises(ValueError,match='reference rate is missing'):
        asyncio.run(data.get_fx('2026-10-03'))
    assert len(calls)==2


# The ECB quotes units per euro (USD 1.1253, JPY 177.82, IDR 18923.45). The ledger stores euros per
# unit, so each is inverted; unrounded, JPY and IDR came to 30 decimal places, past the ledger's
# 28, and save_fx refused the whole batch. On the server not one rate was ever stored, so no
# holding had a euro value and the total never showed (found 18 October 2026).
REAL_ECB = ('<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
            'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube>'
            '<Cube time="2026-10-16"><Cube currency="USD" rate="1.1253"/><Cube currency="JPY" rate="177.82"/>'
            '<Cube currency="IDR" rate="18923.45"/><Cube currency="HUF" rate="388.73"/></Cube>'
            '<Cube time="2026-10-15"><Cube currency="USD" rate="1.1301"/><Cube currency="JPY" rate="178.4"/></Cube>'
            '</Cube></gesmes:Envelope>')


def test_real_ecb_rates_are_stored_whole_including_currencies_with_tiny_inverses():
    from backend.storage import portfolio as S
    rows = data.parse_ecb(REAL_ECB)
    S.save_fx(rows)                          # raised "number exceeds ledger precision" before
    usd = S.fx_rate("USD", "2026-10-16")
    assert usd and abs(Decimal(usd["rate"]) - Decimal(1) / Decimal("1.1253")) < Decimal("1e-15")
    assert S.fx_rate("JPY", "2026-10-15")
    idr = Decimal(next(r["rate"] for r in rows if r["pair"] == "IDR/EUR"))
    assert idr > 0 and abs(idr - Decimal(1) / Decimal("18923.45")) < Decimal("1e-15")


def test_the_data_portal_fallback_rate_is_storable_too():
    rows = [{"pair": "USD/EUR", "day": "2026-10-16", "rate": data.inverse_rate("1.1253"), "source": "ECB", "url": "u"}]
    from backend.storage import portfolio as S
    S.save_fx(rows)
    assert S.fx_rate("USD", "2026-10-16")


def test_a_rate_that_cannot_be_stored_is_logged_not_swallowed(monkeypatch, caplog):
    import logging
    from backend.brain import portfolio as P

    async def failing(day):
        raise ValueError("number exceeds ledger precision")
    monkeypatch.setattr(P.data, "get_fx", failing)
    with caplog.at_level(logging.WARNING, logger=P.log.name):
        with pytest.raises(ValueError):
            asyncio.run(P._rates("2026-10-16"))
    assert "ECB rates for 2026-10-16 not stored" in caplog.text and "precision" in caplog.text
