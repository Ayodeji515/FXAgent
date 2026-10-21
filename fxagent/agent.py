"""Glue: snapshot -> fast decision -> gate -> size -> hard risk check.

The only thing a model contributes is a validated Decision. Thresholds, stops, size and every
veto are computed here in code. A deeper model can be consulted for open positions, but only
to *tighten* (exit); it can never loosen a limit.
"""

from dataclasses import dataclass, replace
from typing import Optional

from .config import GateConfig, RiskLimits, StrategyConfig
from .models import AccountState, Order, Position
from .risk import RiskManager
from .schema import Decision, gate
from .sizing import build_order
from .state_engine import Bar, StateEngine


@dataclass
class AgentAction:
    kind: str                       # "none" | "enter" | "exit"
    reason: str
    order: Optional[Order] = None
    decision: Optional[Decision] = None
    escalated: bool = False
    snapshot: Optional[dict] = None


class Agent:
    def __init__(self, spec, decider, risk: RiskManager, deep_decider=None,
                 gate_cfg: GateConfig = GateConfig(), strat: StrategyConfig = StrategyConfig(),
                 limits: RiskLimits = RiskLimits(), blackouts=()):
        self.spec = spec
        self.decider = decider
        self.deep = deep_decider
        self.risk = risk
        self.gate_cfg = gate_cfg
        self.strat = strat
        self.limits = limits
        self.engine = StateEngine(spec, blackouts=blackouts, atr_period=strat.atr_period)

    def on_bar(self, bar: Bar, now: int, account: AccountState,
               position: Optional[Position] = None) -> AgentAction:
        self.engine.update(bar, now)
        self.risk.evaluate_account(account)

        view = {
            "pos_lots": round(position.direction * position.lots, 2) if position else 0.0,
            "dd_pct": round(account.dd_pct, 2),
            "day_loss_pct": round(account.day_loss_pct, 2),
        }
        snap = self.engine.snapshot(now, view)
        if not snap.get("ready"):
            return AgentAction("none", "warming_up", snapshot=snap)

        # Kill switch always wins: flatten if in a position, otherwise stand down.
        if self.risk.armed:
            if position is not None:
                return AgentAction("exit", f"kill_switch:{self.risk.arm_reason}", snapshot=snap)
            return AgentAction("none", f"kill_switch:{self.risk.arm_reason}", snapshot=snap)

        fast = self.decider.decide(snap)

        if position is not None:
            return self._manage(position, fast, snap)
        return self._enter(fast, snap, account)

    # --- open position -----------------------------------------------------
    def _manage(self, position, fast, snap):
        crisis = fast.regime == "crisis"
        low_conf = fast.confidence < self.gate_cfg.escalate_confidence
        if self.deep is not None and (crisis or low_conf):
            deep = self.deep.decide(snap)
            pos_dir = "long" if position.direction > 0 else "short"
            if (deep.regime == "crisis" or deep.confidence < self.gate_cfg.escalate_confidence
                    or deep.direction != pos_dir):
                return AgentAction("exit", "deep_model_exit", decision=deep, escalated=True, snapshot=snap)
            return AgentAction("none", "deep_model_hold", decision=deep, escalated=True, snapshot=snap)
        if crisis:
            # No deep model configured: the safe default in a crisis regime is to flatten.
            return AgentAction("exit", "crisis_flatten", decision=fast, escalated=True, snapshot=snap)
        # Otherwise the hard stop and target already bound the loss; hold.
        return AgentAction("none", "hold", decision=fast, snapshot=snap)

    # --- flat --------------------------------------------------------------
    def _enter(self, fast, snap, account):
        code_state = self.risk.risk_state(account)
        ok, why = gate(fast, code_state, self.gate_cfg)
        if not ok:
            return AgentAction("none", why, decision=fast, snapshot=snap)

        order, why = build_order(fast, snap, self.spec, account.equity, self.gate_cfg, self.strat, self.limits)
        if order is None:
            return AgentAction("none", why, decision=fast, snapshot=snap)

        verdict = self.risk.check_order(order, account, self.spec, snap)
        if not verdict.allowed:
            return AgentAction("none", "risk:" + ",".join(verdict.reasons), decision=fast, snapshot=snap)
        if verdict.lots != order.lots:
            order = replace(order, risk_usd=order.risk_usd * verdict.lots / order.lots, lots=verdict.lots)
        return AgentAction("enter", "ok", order=order, decision=fast, snapshot=snap)
