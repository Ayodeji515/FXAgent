"""Typed decision schema. Anything a model returns is validated here or discarded."""

import math
from dataclasses import dataclass, fields

REGIMES = ("trending", "mean_reverting", "high_vol", "crisis")
DIRECTIONS = ("long", "short", "neutral")
RISK_STATES = ("safe", "near_limit", "reduce")


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    regime: str
    direction: str
    setup_quality: int        # 0..3
    risk_state: str           # the model's view; code computes the authoritative one
    confidence: float         # 0..1
    p_win: float              # calibrated probability the target is hit before the stop

    def __post_init__(self):
        if self.regime not in REGIMES:
            raise SchemaError(f"regime must be one of {REGIMES}, got {self.regime!r}")
        if self.direction not in DIRECTIONS:
            raise SchemaError(f"direction must be one of {DIRECTIONS}, got {self.direction!r}")
        if self.risk_state not in RISK_STATES:
            raise SchemaError(f"risk_state must be one of {RISK_STATES}, got {self.risk_state!r}")
        q = self.setup_quality
        if isinstance(q, bool) or not isinstance(q, int) or not 0 <= q <= 3:
            raise SchemaError(f"setup_quality must be an int 0..3, got {q!r}")
        for name in ("confidence", "p_win"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v) or not 0.0 <= v <= 1.0:
                raise SchemaError(f"{name} must be a number in [0, 1], got {v!r}")

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise SchemaError("decision must be a JSON object")
        try:
            return cls(**{f.name: d[f.name] for f in fields(cls)})
        except KeyError as e:
            raise SchemaError(f"missing field {e}") from None

    @classmethod
    def hold(cls, regime="high_vol"):
        """Safe default when a model fails, times out or returns garbage."""
        return cls(regime=regime, direction="neutral", setup_quality=0,
                   risk_state="reduce", confidence=0.0, p_win=0.0)


_RANK = {"safe": 0, "near_limit": 1, "reduce": 2}


def most_conservative(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def gate(decision: Decision, code_risk_state: str, cfg):
    """Fire only when quality, confidence and (code-computed) risk state all pass.

    Returns (ok, reason). The effective risk state is the more conservative of the
    model's view and the code's, so a model can never talk its way past the code.
    """
    if decision.direction == "neutral":
        return False, "neutral"
    if decision.regime == "crisis":
        return False, "crisis_regime"
    if decision.setup_quality < cfg.min_setup_quality:
        return False, "low_setup_quality"
    if not decision.confidence > cfg.min_confidence:
        return False, "low_confidence"
    if most_conservative(decision.risk_state, code_risk_state) != "safe":
        return False, "risk_state_not_safe"
    return True, "ok"
