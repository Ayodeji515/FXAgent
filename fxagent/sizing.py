"""Fractional Kelly sizing and ATR-based stops. Every number is computed in code."""

import math

from .config import GateConfig, RiskLimits, StrategyConfig
from .models import Order
from .schema import Decision


def kelly(p: float, b: float) -> float:
    """Full-Kelly fraction for win probability p and net payoff ratio b. Negative = no edge."""
    return p - (1.0 - p) / b


def build_order(decision: Decision, snap: dict, spec, equity: float,
                gate: GateConfig = GateConfig(), strat: StrategyConfig = StrategyConfig(),
                limits: RiskLimits = RiskLimits()):
    """Return (Order | None, reason). Stops/targets come from ATR, not from the model."""
    direction = 1 if decision.direction == "long" else -1
    atr = snap["atr"]
    spread = snap["spread"]
    stop_d = strat.atr_stop_mult * atr
    tp_d = strat.atr_tp_mult * atr

    # Spread is paid on entry and exit; model it as shrinking the reward and widening the risk.
    eff_reward = tp_d - spread
    eff_risk = stop_d + spread
    if eff_reward <= 0:
        return None, "costs_exceed_target"
    b = eff_reward / eff_risk
    f = kelly(decision.p_win, b)
    if f <= 0:
        return None, "no_edge_after_costs"

    risk_frac = min(gate.kelly_fraction * f, limits.max_risk_per_trade_pct / 100.0)
    per_lot_risk = eff_risk * spec.contract_size
    lots = math.floor(equity * risk_frac / per_lot_risk / spec.lot_step + 1e-9) * spec.lot_step
    lots = min(round(lots, 8), spec.max_lot)
    if lots < spec.min_lot:
        return None, "below_min_lot"

    ref = snap["mid"]
    return Order(
        symbol=spec.symbol,
        direction=direction,
        lots=lots,
        entry_ref=ref,
        stop=ref - direction * stop_d,
        tp=ref + direction * tp_d,
        risk_usd=lots * per_lot_risk,
    ), "ok"
