"""Overnight / global context from Yahoo daily candles (free, unofficial, delayed)."""
import logging

import requests

log = logging.getLogger(__name__)
SYMBOLS = [("India VIX", "^INDIAVIX"), ("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("Nikkei 225", "^N225"),
           ("Hang Seng", "^HSI"), ("Brent crude", "BZ=F"), ("USD/INR", "USDINR=X")]
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{s}"


def fetch_context(session=None, timeout=15) -> tuple:
    """Return (rows, errors); each row = {name, last, prev, pct}."""
    s = session or requests.Session()
    rows, errors = [], []
    for name, sym in SYMBOLS:
        try:
            r = s.get(URL.format(s=sym), params={"interval": "1d", "range": "5d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            r.raise_for_status()
            closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
            if len(closes) < 2:
                raise ValueError("not enough points")
            rows.append({"name": name, "last": closes[-1], "prev": closes[-2], "pct": 100 * (closes[-1] / closes[-2] - 1)})
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as e:
            errors.append(f"{name}: {type(e).__name__}")
    return rows, errors


def read_context(rows) -> list:
    """Plain observations (facts about the move, not predictions)."""
    by = {r["name"]: r for r in rows}
    notes = []
    us = [by[n]["pct"] for n in ("S&P 500", "Nasdaq") if n in by]
    if us:
        avg = sum(us) / len(us)
        notes.append("US markets closed " + ("clearly lower" if avg <= -1 else "lower" if avg < -0.3 else "higher" if avg > 0.3 else "little changed")
                     + f" (avg {avg:+.2f}%)")
    asia = [by[n]["pct"] for n in ("Nikkei 225", "Hang Seng") if n in by]
    if asia:
        a = sum(asia) / len(asia)
        notes.append("Asia " + ("weak" if a < -0.5 else "strong" if a > 0.5 else "mixed") + f" (avg {a:+.2f}%)")
    if "India VIX" in by:
        v = by["India VIX"]
        notes.append(f"India VIX {v['last']:.1f} ({v['pct']:+.1f}%): " + ("fear rising, bigger swings likely" if v["pct"] > 5 else
                                                                         "fear falling" if v["pct"] < -5 else "no big change"))
    if "Brent crude" in by and abs(by["Brent crude"]["pct"]) >= 2:
        notes.append(f"Brent crude moved {by['Brent crude']['pct']:+.1f}% (large)")
    if "USD/INR" in by and abs(by["USD/INR"]["pct"]) >= 0.4:
        notes.append(f"Rupee moved sharply: USD/INR {by['USD/INR']['pct']:+.2f}%")
    return notes
