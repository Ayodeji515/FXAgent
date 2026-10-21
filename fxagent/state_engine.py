"""Deterministic state engine: bars in, one compact numeric snapshot out.

Causality rules enforced here:
  * a bar can only be added if its close time is <= `now`
  * bar timestamps must strictly increase
  * the snapshot is computed only from bars already added
Every feature is computed in code. The decision model only ever sees the snapshot.
"""

import json
import math
from collections import deque
from dataclasses import dataclass

MIN_BARS = 40


class CausalityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bar:
    ts: int                 # bar CLOSE time, epoch seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0     # average spread over the bar in price units; 0 = unknown

    def __post_init__(self):
        lo, hi = min(self.open, self.close), max(self.open, self.close)
        if not (self.low <= lo and hi <= self.high):
            raise ValueError(f"inconsistent OHLC at ts={self.ts}")


def blackout_window(event_ts: int, before: int = 1800, after: int = 1800):
    """Helper for scheduled events (CPI, FOMC, BoE, NFP): no new entries inside the window."""
    return (event_ts - before, event_ts + after)


def _session(hour_utc: int) -> str:
    if 0 <= hour_utc < 7:
        return "asia"
    if 7 <= hour_utc < 12:
        return "ldn"
    if 12 <= hour_utc < 16:
        return "ovl"
    if 16 <= hour_utc < 21:
        return "ny"
    return "off"


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs):
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


class StateEngine:
    def __init__(self, spec, history: int = 200, blackouts=(), atr_period: int = 14):
        self.spec = spec
        self.bars = deque(maxlen=history)
        self.blackouts = tuple(blackouts)
        self.atr_period = atr_period
        self._decimals = max(0, -int(math.floor(math.log10(spec.tick))))

    def update(self, bar: Bar, now: int):
        if bar.ts > now:
            raise CausalityError(f"bar ts {bar.ts} is in the future relative to now={now}")
        if self.bars and bar.ts <= self.bars[-1].ts:
            raise CausalityError("bar timestamps must strictly increase")
        self.bars.append(bar)

    def _atr(self):
        n = self.atr_period
        if len(self.bars) < n + 1:
            return None
        bars = list(self.bars)[-(n + 1):]
        trs = []
        for prev, cur in zip(bars[:-1], bars[1:]):
            trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        return _mean(trs)

    def snapshot(self, now: int, acct: dict | None = None) -> dict:
        if not self.bars:
            return {"ready": False, "n": 0}
        last = self.bars[-1]
        if now < last.ts:
            raise CausalityError("snapshot time precedes the last bar")
        n = len(self.bars)
        if n < MIN_BARS:
            return {"ready": False, "n": n}

        closes = [b.close for b in self.bars]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
        d = self._decimals
        atr = self._atr()
        spread = last.spread if last.spread > 0 else self.spec.typical_spread

        sma20 = _mean(closes[-20:])
        sd20 = _std(closes[-20:])
        z = (closes[-1] - sma20) / sd20 if sd20 > 0 else 0.0
        slope = (_mean(closes[-10:]) - _mean(closes[-30:])) / atr if atr else 0.0
        rv_s = _std(rets[-20:]) * 1e4
        rv_l = _std(rets[-min(len(rets), 100):]) * 1e4
        rng = last.high - last.low
        pressure = (last.close - last.open) / rng if rng > 0 else 0.0

        def ret_bps(k):
            return round((closes[-1] / closes[-1 - k] - 1.0) * 1e4, 2)

        snap = {
            "ready": True,
            "sym": self.spec.symbol,
            "mid": round(closes[-1], d),
            "spread": round(spread, d + 1),
            "spread_x": round(spread / self.spec.typical_spread, 2),
            "atr": round(atr, d + 1),
            "r1": ret_bps(1), "r5": ret_bps(5), "r15": ret_bps(15),
            "rv": round(rv_s, 2),
            "rv_ratio": round(rv_s / rv_l, 2) if rv_l > 0 else 1.0,
            "z20": round(z, 2),
            "slope": round(slope, 2),
            "pressure": round(pressure, 2),
            "session": _session((now % 86400) // 3600),
            "blackout": any(s <= now <= e for s, e in self.blackouts),
            "age_s": int(now - last.ts),
        }
        if acct:
            snap.update({k: round(v, 3) if isinstance(v, float) else v for k, v in acct.items()})
        return snap


def approx_tokens(snapshot: dict) -> int:
    """Rough token estimate (about 3 characters per token for dense JSON)."""
    return len(json.dumps(snapshot, separators=(",", ":"))) // 3
