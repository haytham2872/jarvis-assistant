"""L0: public feeds only; no model calls, keys printed, or runtime writes.

Run from BOTH the desk and GCP: python scripts/measure_portfolio_sources.py
The JSON output is evidence, not an assertion that feeds agree.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import platform
import argparse
import sys
from pathlib import Path
import time
from datetime import date, timedelta
from decimal import Decimal
from xml.etree import ElementTree as ET

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.market import data as market_data


async def measure(*, include_europe=False) -> dict:
    end = date.today()
    start = end - timedelta(days=45)
    requests = []
    for symbol in ("NVDA", "TTWO", "TSLA"):
        requests.extend([
            (symbol, "yahoo", f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d&events=splits"),
            (symbol, "stooq", f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d&d1={start:%Y%m%d}&d2={end:%Y%m%d}"),
            (symbol, "nasdaq", f"https://api.nasdaq.com/api/quote/{symbol}/historical?assetclass=stocks&fromdate={start}&todate={end}&limit=30"),
        ])
    european={"AIR.PA":"XPAR","MC.PA":"XPAR","ASML.AS":"XAMS"}
    if include_europe:
        requests.extend((symbol,'yahoo',f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d&events=splits')
                        for symbol in european)
    requests.append(("USD/EUR", "ecb", "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"))
    async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/csv,*/*"}) as client:
        async def fetch(symbol, source, url):
            t = time.perf_counter()
            result = {"symbol": symbol, "source": source, "url": url}
            try:
                r = await client.get(url)
                result["status"] = r.status_code
                r.raise_for_status()
                rows = []
                if source == "yahoo":
                    chart = r.json()["chart"]["result"][0]
                    expected_currency='EUR' if symbol in european else 'USD'
                    if chart['meta']['symbol'].upper()!=symbol or chart['meta']['currency']!=expected_currency:
                        raise ValueError('feed symbol/currency mismatch')
                    for ts, close in zip(chart.get("timestamp", []), chart["indicators"]["quote"][0]["close"]):
                        if close is not None:
                            from datetime import datetime
                            from zoneinfo import ZoneInfo
                            value=market_data.number(close)
                            for split in chart.get('events',{}).get('splits',{}).values():
                                if split['date']>ts:
                                    value*=market_data.number(split['numerator'])/market_data.number(split['denominator'])
                            rows.append({"date": datetime.fromtimestamp(ts, ZoneInfo(chart["meta"]["exchangeTimezoneName"])).date().isoformat(), "close": str(value)})
                    result["currency"] = chart["meta"]["currency"]
                    result["splits"] = chart.get("events", {}).get("splits", {})
                elif source == "stooq":
                    rows = [{"date": row["Date"], "close": row["Close"]} for row in csv.DictReader(io.StringIO(r.text))]
                elif source == "nasdaq":
                    data = r.json().get("data") or {}
                    from datetime import datetime
                    rows = [{"date": datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat(),
                             "close": row["close"].replace("$", "").replace(",", "")} for row in data.get("tradesTable", {}).get("rows", [])]
                else:
                    for node in ET.fromstring(r.text).iter():
                        if "time" in node.attrib:
                            usd = next((c.attrib["rate"] for c in node if c.attrib.get("currency") == "USD"), None)
                            if usd:
                                rows.append({"date": node.attrib["time"], "close": str(Decimal(1) / Decimal(usd))})
                if source in ("yahoo","stooq","nasdaq"):
                    from datetime import datetime
                    i={"exchange":european.get(symbol,'NASDAQ')}
                    rows=[row for row in rows if market_data.fetch_at(i,row["date"]) is not None
                          and market_data.fetch_at(i,row["date"]) <= datetime.now(market_data.LOCAL_TZ)]
                result["closes"] = sorted(rows, key=lambda row: row["date"])[-20:]
                result["count"] = len(result["closes"])
                if not rows:
                    result["error"] = "No structured closes (blocked or unavailable)"
            except Exception as exc:
                result["error"] = type(exc).__name__ + ": " + str(exc).split("?")[0][:150]
            result["latency_s"] = round(time.perf_counter() - t, 3)
            return result
        results = await asyncio.gather(*(fetch(*args) for args in requests))
    comparisons = []
    for symbol in ("NVDA", "TTWO", "TSLA"):
        feeds = {r["source"]: {c["date"]: Decimal(c["close"]) for c in r.get("closes", [])}
                 for r in results if r["symbol"] == symbol}
        primary = feeds.get("nasdaq", {})
        for source in ("yahoo", "stooq"):
            common = primary.keys() & feeds.get(source, {}).keys()
            comparisons.append({"symbol": symbol, "source": source, "exchange_overlap": len(common),
                                "max_difference": str(max((abs(primary[d] - feeds[source][d]) for d in common), default=Decimal(0))) if common else None})
    return {"host":platform.system(),"date":str(end),"results":results,"comparisons":comparisons,
            "europe_exchange_comparison":"not performed; Yahoo availability and identity/parsing only" if include_europe else 'not requested',
            "gcp_verified":False,"keyed_candidates":"not configured/probed; no new accounts or keys required"}


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--output",type=Path)
    args.add_argument('--include-europe',action='store_true',help='Also check EUR Paris/Amsterdam feeds; this does not verify prices against a European exchange.')
    options=args.parse_args()
    report=asyncio.run(measure(include_europe=options.include_europe))
    if options.output:
        options.output.parent.mkdir(parents=True,exist_ok=True)
        options.output.write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps({"results":[{k:v for k,v in r.items() if k not in ("closes","splits")} for r in report['results']],
                          "comparisons":report['comparisons']},indent=2))
    else:
        print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
