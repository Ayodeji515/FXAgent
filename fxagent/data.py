"""Bar sources: a synthetic generator (plumbing tests only) and a CSV loader for real data."""

import csv
import math
import random

from .state_engine import Bar


def synthetic_bars(spec, n=5000, seed=1, start_ts=1_700_000_000, bar_seconds=3600,
                   start_price=None, ann_vol=None, momentum=0.0):
    """Regime-switching random walk. With momentum=0 there is NO edge by construction, so a
    backtest on it should lose roughly the trading costs. That is a useful sanity check.
    Do not read anything about real-market performance from this data."""
    rng = random.Random(seed)
    if start_price is None:
        start_price = 2000.0 if spec.symbol == "XAUUSD" else 1.30
    if ann_vol is None:
        ann_vol = 0.18 if spec.symbol == "XAUUSD" else 0.07
    base_sigma = ann_vol / math.sqrt(24 * 252)
    mult, prev_ret, price, ts = 1.0, 0.0, start_price, start_ts
    bars = []
    for _ in range(n):
        if rng.random() < 0.01:
            mult = rng.choice([0.7, 1.0, 1.0, 2.5])
        sigma = base_sigma * mult
        ret = momentum * prev_ret + rng.gauss(0.0, sigma)
        prev_ret = ret
        o = price
        c = o * math.exp(ret)
        wick_hi = abs(rng.gauss(0.0, sigma * 0.5)) * o
        wick_lo = abs(rng.gauss(0.0, sigma * 0.5)) * o
        h = max(o, c) + wick_hi
        l = min(o, c) - wick_lo
        ts += bar_seconds
        spread = spec.typical_spread * (1.0 + 0.5 * (mult > 1.5))
        bars.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=0.0, spread=spread))
        price = c
    return bars


def load_csv(path, bar_seconds=None):
    """CSV columns: ts (epoch seconds, bar CLOSE time), open, high, low, close[, volume][, spread].
    If your file stores bar OPEN time, pass bar_seconds so timestamps become close times."""
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = int(float(row["ts"])) + (bar_seconds or 0)
            bars.append(Bar(ts=ts, open=float(row["open"]), high=float(row["high"]),
                            low=float(row["low"]), close=float(row["close"]),
                            volume=float(row.get("volume") or 0.0),
                            spread=float(row.get("spread") or 0.0)))
    return bars
