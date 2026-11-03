"""Daily *unadjusted* closes and dated ECB reference FX. Never accept a live quote
as a completed session. A feed failure is a missing mark, not a guessed number.

Amounts are totalled in the owner's base currency, JARVIS_BASE_CURRENCY (default EUR).
The ECB quotes every currency against the euro; a rate stored as the pair "X/BASE" is
units of BASE per unit of X, a cross rate through the euro when BASE is not the euro.
"""
from __future__ import annotations
from backend import localtime as _localtime

import csv
import io
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from urllib.parse import quote
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

LOCAL_TZ = _localtime.ZONE


def _base_currency() -> str:
    value = os.environ.get("JARVIS_BASE_CURRENCY", "").strip()
    if not value:
        try:
            from dotenv import dotenv_values
            from pathlib import Path
            value = (dotenv_values(Path(__file__).resolve().parents[2] / ".env")
                     .get("JARVIS_BASE_CURRENCY") or "").strip()
        except Exception:
            value = ""
    value = value.upper()
    return value if len(value) == 3 and value.isalpha() else "EUR"


BASE = _base_currency()            # the currency every total is in
CALENDARS = {"NASDAQ": "XNYS", "NYSE": "XNYS", "XPAR": "XPAR", "XAMS": "XAMS", "XETR": "XETR"}
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/csv,*/*"}


def number(value, *, zero=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("expected a decimal number") from None
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ValueError("expected a finite positive number")
    if result > Decimal("1e15") or result.as_tuple().exponent < -28:
        raise ValueError("number exceeds ledger precision")
    return result


# Euros per unit, from the ECB's units per euro. Rounded to 18 places: the ECB quotes 4-6
# significant digits, and an unrounded inverse (JPY, IDR: 30 places) is past the 28 that number()
# accepts, which made save_fx refuse every batch and left the ledger with no rate at all.
_RATE_PLACES = Decimal("1e-18")


def inverse_rate(per_euro) -> str:
    return format((Decimal(1) / number(per_euro)).quantize(_RATE_PLACES).normalize(), "f")


def cross_rates(per_euro: dict[str, str], base: str | None = None) -> dict[str, str]:
    """{currency: units of `base` per unit of it} from the ECB's {currency: units per euro}.
    Every currency quoted, and the euro itself when the base is not the euro; {} when the
    base is not quoted that day."""
    base = base or BASE
    units = {"EUR": Decimal(1), **{c: number(r) for c, r in per_euro.items()}}
    if base not in units:
        return {}
    return {c: format((units[base] / u).quantize(_RATE_PLACES).normalize(), "f")
            for c, u in units.items() if c != base}


@lru_cache(maxsize=20)
def calendar(exchange: str, year: int):
    import exchange_calendars as xcals
    if exchange not in CALENDARS:
        raise ValueError(f"unsupported exchange {exchange}; provide NASDAQ, NYSE, XPAR, XAMS or XETR")
    return xcals.get_calendar(CALENDARS[exchange], start=f"{year-1}-01-01", end=f"{year+2}-12-31")


def fetch_at(instrument: dict, day: str) -> datetime | None:
    d = date.fromisoformat(day)
    cal = calendar(instrument["exchange"], d.year)
    if not cal.is_session(day):
        return None
    return (cal.session_close(day).to_pydatetime() + timedelta(minutes=20)).astimezone(LOCAL_TZ)


def sessions(instrument: dict, start: str, end: str) -> list[str]:
    cal = calendar(instrument["exchange"], date.fromisoformat(start).year)
    return [s.date().isoformat() for s in cal.sessions_in_range(start, end)]


def parse_yahoo(body: dict, instrument: dict) -> list[dict]:
    chart = body["chart"]["result"][0]
    meta = chart["meta"]
    if meta["symbol"].upper() != instrument["price_key"].upper() or meta["currency"] != instrument["currency"]:
        raise ValueError("feed symbol/currency does not match instrument")
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    splits = list(chart.get("events", {}).get("splits", {}).values())
    quotes = chart["indicators"]["quote"][0]["close"]
    times = chart.get("timestamp", [])
    if len(times) != len(quotes):
        raise ValueError("misaligned close timestamps")
    out = []
    for ts, raw in zip(times, quotes):
        if raw is None:
            continue
        value = number(raw)
        # Yahoo historical close is split-adjusted. Reconstruct the then-traded price;
        # adjclose (which also includes dividends) must never be used for valuation.
        for split in splits:
            if float(split["date"]) > ts:
                value *= number(split["numerator"]) / number(split["denominator"])
        day = datetime.fromtimestamp(float(ts), zone).date().isoformat()
        out.append({"day": day, "close": str(value.quantize(Decimal("0.01"))), "currency": meta["currency"]})
    return out


def parse_nasdaq(body: dict, instrument: dict) -> list[dict]:
    if instrument["currency"] != "USD" or instrument["exchange"] not in ("NASDAQ", "NYSE"):
        raise ValueError("Nasdaq source only supports US USD instruments")
    data = body.get("data") or {}
    out = []
    for row in (data.get("tradesTable") or {}).get("rows") or []:
        value = number(row["close"].replace("$", "").replace(",", ""))
        out.append({"day": datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat(),
                    "close": str(value), "currency": "USD"})
    return out


def parse_ecb(body: str, base: str | None = None) -> list[dict]:
    base = base or BASE
    out = []
    for node in ET.fromstring(body).iter():
        if "time" not in node.attrib:
            continue
        day = date.fromisoformat(node.attrib["time"]).isoformat()
        per_euro = {child.attrib["currency"]: child.attrib["rate"] for child in node
                    if child.attrib.get("currency")}
        for currency, rate in cross_rates(per_euro, base).items():
            out.append({"pair": f"{currency}/{base}", "day": day, "rate": rate,
                        "source": "ECB reference", "url": ECB_URL})
    if not out:
        raise ValueError("no dated FX rates")
    return out


async def get_closes(instrument: dict, day: str) -> dict:
    """Primary then fallback, requiring the requested completed session and currency."""
    due = fetch_at(instrument, day)
    if due is None or datetime.now(LOCAL_TZ) < due:
        raise ValueError("session has not closed plus 20 minutes")
    key = quote(instrument["price_key"], safe="")
    urls = {
        "nasdaq": f"https://api.nasdaq.com/api/quote/{key}/historical?assetclass=stocks&fromdate={(date.fromisoformat(day)-timedelta(days=7)).isoformat()}&todate={day}&limit=20",
        "yahoo": f"https://query1.finance.yahoo.com/v8/finance/chart/{key}?range=3mo&interval=1d&events=splits",
    }
    defaults = "nasdaq,yahoo" if instrument["exchange"] in ("NASDAQ", "NYSE") else "yahoo"
    sources = (os.getenv("JARVIS_PORTFOLIO_SOURCES") or defaults).split(",")
    errors = []
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=HEADERS) as client:
        for source in dict.fromkeys(s.strip() for s in sources):
            try:
                url = urls[source]
                response = await client.get(url)
                response.raise_for_status()
                body = json.loads(response.text, parse_float=Decimal)
                rows = parse_nasdaq(body, instrument) if source == "nasdaq" else parse_yahoo(body, instrument)
                match = next((row for row in rows if row["day"] == day), None)
                if match is None:
                    raise ValueError("requested close is missing")
                return {**match, "source": source, "url": url}
            except Exception as exc:
                errors.append(f"{source}: {type(exc).__name__}")
    raise ValueError("all price sources failed (" + ", ".join(errors) + ")")


def parse_splits(body: dict, instrument: dict, url: str) -> list[dict]:
    """Splits from Yahoo's chart events. The event's `date` is the ex-date's market open, so
    its day is read in the exchange's own time zone (checked live: NVDA's 10-for-1 reads
    2024-06-10, its 4-for-1 2021-07-20)."""
    chart = body["chart"]["result"][0]
    meta = chart["meta"]
    if meta["symbol"].upper() != instrument["price_key"].upper():
        raise ValueError("feed symbol does not match instrument")
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    out = []
    for event in (chart.get("events") or {}).get("splits", {}).values():
        num, den = number(event["numerator"]), number(event["denominator"])
        if num == den:
            continue
        out.append({"day": datetime.fromtimestamp(float(event["date"]), zone).date().isoformat(),
                    "numerator": format(num.normalize(), "f"), "denominator": format(den.normalize(), "f"),
                    "source": "yahoo", "url": url})
    return sorted(out, key=lambda s: s["day"])


async def get_splits(instrument: dict, since: str) -> list[dict]:
    """Every split from `since` to today. Monthly bars keep the payload small (~10 KB for five
    years); the split events come with any interval."""
    start = int(datetime.combine(date.fromisoformat(since) - timedelta(days=1), datetime.min.time(), LOCAL_TZ).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(instrument['price_key'], safe='')}"
    params = {"period1": start, "period2": int(datetime.now(LOCAL_TZ).timestamp()), "interval": "1mo", "events": "splits"}
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=HEADERS) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return parse_splits(json.loads(response.text, parse_float=Decimal), instrument, str(response.url))


async def get_fx(day: str) -> list[dict]:
    """ECB XML with ECB's separate Data Portal endpoint as the USD fallback."""
    end = date.fromisoformat(day).isoformat()
    start = (date.fromisoformat(day)-timedelta(days=7)).isoformat()
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=HEADERS) as client:
        try:
            response = await client.get(ECB_URL)
            response.raise_for_status()
            rows = parse_ecb(response.text)
            eligible = [r for r in rows if start <= r["day"] <= end]
            if not eligible:
                raise ValueError("requested ECB reference window is missing")
            # ECB publishes on business days. Keep the actual reference date for
            # weekend/holiday trades, including all currencies in the XML feed.
            return eligible
        except Exception:
            # The fallback quotes one currency per request: USD, and the base when it is
            # not the euro, so USD/BASE and EUR/BASE can be crossed.
            series: dict[str, dict[str, str]] = {}
            url = ""
            for currency in sorted({"USD", BASE} - {"EUR"}):
                url = f"https://data-api.ecb.europa.eu/service/data/EXR/D.{currency}.EUR.SP00.A"
                response = await client.get(url, params={"startPeriod": start, "endPeriod": day,
                                                         "format": "csvdata"})
                response.raise_for_status()
                for r in csv.DictReader(io.StringIO(response.text)):
                    if start <= r["TIME_PERIOD"] <= end:
                        series.setdefault(date.fromisoformat(r["TIME_PERIOD"]).isoformat(),
                                          {})[currency] = r["OBS_VALUE"]
                url = str(response.url)
            rows = [{"pair": f"{c}/{BASE}", "day": d, "rate": rate, "source": "ECB reference", "url": url}
                    for d, per_euro in sorted(series.items())
                    for c, rate in cross_rates(per_euro).items()]
            if not rows:
                raise ValueError("requested ECB reference rate is missing")
            return rows


# ---------------------------------------------------------------- display history (phone charts)
# For drawing only, never for valuation: Yahoo's chart closes as given, which are adjusted for
# every split up to today, so a year of prices reads in today's shares. The ledger's own marks
# (unadjusted, dated) stay the source of every figure.

def parse_history(body: dict, instrument: dict) -> list[dict]:
    """Split-adjusted daily (or weekly) closes, as Yahoo gives them."""
    chart = body["chart"]["result"][0]
    meta = chart["meta"]
    if meta["symbol"].upper() != instrument["price_key"].upper() or meta["currency"] != instrument["currency"]:
        raise ValueError("feed symbol/currency does not match instrument")
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    quotes = chart["indicators"]["quote"][0]["close"]
    times = chart.get("timestamp", [])
    if len(times) != len(quotes):
        raise ValueError("misaligned close timestamps")
    out: dict[str, str] = {}
    for ts, raw in zip(times, quotes):
        if raw is None:
            continue
        day = datetime.fromtimestamp(float(ts), zone).date().isoformat()
        out[day] = format(number(raw).quantize(Decimal("0.0001")).normalize(), "f")
    return [{"day": d, "close": c} for d, c in sorted(out.items())]


async def get_history(instrument: dict, weekly: bool = False) -> list[dict]:
    """A year of daily closes, or every week since listing."""
    params = {"range": "max", "interval": "1wk"} if weekly else {"range": "1y", "interval": "1d"}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(instrument['price_key'], safe='')}"
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=HEADERS) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        rows = parse_history(json.loads(response.text, parse_float=Decimal), instrument)
    if not rows:
        raise ValueError("no closes in the history")
    return rows


def parse_fx_history(text: str, currency: str) -> list[dict]:
    """ECB Data Portal CSV: euros per unit of `currency`, by reference date (the euro leg;
    get_fx_history crosses it into the base)."""
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            rows.append({"day": date.fromisoformat(r["TIME_PERIOD"]).isoformat(),
                         "rate": format((Decimal(1) / number(r["OBS_VALUE"])).quantize(Decimal("1e-8")).normalize(), "f")})
        except (KeyError, ValueError):
            continue
    if not rows:
        raise ValueError(f"no {currency}/EUR reference rates")  # the euro leg, before crossing
    return sorted(rows, key=lambda r: r["day"])


async def _euro_history(client, currency: str, start: str, end: str) -> list[dict]:
    url = f"https://data-api.ecb.europa.eu/service/data/EXR/D.{quote(currency, safe='')}.EUR.SP00.A"
    response = await client.get(url, params={"startPeriod": start, "endPeriod": end, "format": "csvdata"})
    response.raise_for_status()
    return parse_fx_history(response.text, currency)


def cross_history(euro_per_unit: list[dict] | None, euro_per_base: list[dict] | None) -> list[dict]:
    """Base per unit of a currency, by date, from the two euro legs (None: that leg is the
    euro itself). Only dates both legs have."""
    if euro_per_base is None:
        return list(euro_per_unit or [])
    base = {r["day"]: Decimal(r["rate"]) for r in euro_per_base}
    unit = ({r["day"]: Decimal(r["rate"]) for r in euro_per_unit} if euro_per_unit is not None
            else {d: Decimal(1) for d in base})
    return [{"day": d, "rate": format((unit[d] / base[d]).quantize(Decimal("1e-8")).normalize(), "f")}
            for d in sorted(unit) if d in base]


async def get_fx_history(currency: str, start: str, end: str) -> list[dict]:
    """Base per unit of `currency` over a range (the daily feed above keeps only 90 days)."""
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=HEADERS) as client:
        unit = None if currency == "EUR" else await _euro_history(client, currency, start, end)
        base = None if BASE == "EUR" else await _euro_history(client, BASE, start, end)
    rows = cross_history(unit, base)
    if not rows:
        raise ValueError(f"no {currency}/{BASE} reference rates")
    return rows
