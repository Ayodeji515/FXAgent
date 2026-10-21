"""Pluggable fast decision layer.

A Decider maps one compact snapshot to a typed Decision. Plug in any model you like:
wrap a function that takes the snapshot JSON string and returns a JSON-like dict with
CallableDecider. Failures, timeouts and malformed output all collapse to a safe HOLD.

BaselineDecider is a transparent rule-based stand-in so the whole pipeline runs and can be
tested without any external model. It is NOT claimed to have an edge, and its p_win values
are placeholders until the nightly review has real outcomes to calibrate against.
"""

import json
from typing import Callable, Protocol

from .schema import Decision, SchemaError


class Decider(Protocol):
    def decide(self, snapshot: dict) -> Decision: ...


class CallableDecider:
    """Wrap any model call: fn(snapshot_json: str) -> dict matching the Decision fields."""

    def __init__(self, fn: Callable[[str], dict], name: str = "model"):
        self.fn = fn
        self.name = name
        self.failures = 0

    def decide(self, snapshot: dict) -> Decision:
        try:
            raw = self.fn(json.dumps(snapshot, separators=(",", ":")))
            return Decision.from_dict(raw)
        except (SchemaError, Exception):
            self.failures += 1
            return Decision.hold()


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


class BaselineDecider:
    def __init__(self, calibration: dict | None = None):
        cal = calibration or {}
        self.alpha = cal.get("alpha", 1.0)
        self.bias = cal.get("bias", 0.0)

    def _calibrate(self, p):
        return _clip(0.5 + self.alpha * (p - 0.5) + self.bias)

    def decide(self, snap: dict) -> Decision:
        if not snap.get("ready"):
            return Decision.hold()

        rvr, z, slope = snap["rv_ratio"], snap["z20"], snap["slope"]
        if snap["spread_x"] > 4.0 or rvr > 3.5:
            regime = "crisis"
        elif rvr > 2.0:
            regime = "high_vol"
        elif abs(slope) >= 1.0:
            regime = "trending"
        else:
            regime = "mean_reverting"

        direction, quality, strength = "neutral", 0, 0.0
        if regime == "trending":
            direction = "long" if slope > 0 else "short"
            strength = abs(slope)
            quality = 3 if strength >= 2.0 else 2 if strength >= 1.5 else 1
            if (snap["r5"] > 0) != (slope > 0):
                quality = min(quality, 1)       # short-term move disagrees with the trend
        elif regime == "mean_reverting" and abs(z) >= 1.8:
            direction = "short" if z > 0 else "long"
            strength = abs(z) - 1.0
            quality = 3 if abs(z) >= 2.5 else 2

        if direction == "neutral":
            p_win, conf = 0.0, 0.5
        else:
            p_win = self._calibrate(0.42 + 0.04 * quality)
            conf = min(0.95, 0.5 + 0.1 * quality + 0.05 * min(strength, 3.0))
        return Decision(regime=regime, direction=direction, setup_quality=quality,
                        risk_state="safe", confidence=round(conf, 3), p_win=round(p_win, 3))
