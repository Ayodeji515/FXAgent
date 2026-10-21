"""Hard deterministic risk layer. Checked before every order; nothing here reads model output
except the order the sizer built, and every limit comes from config."""

import math
import os
from dataclasses import dataclass, field

from .config import RiskLimits
from .models import AccountState, Order


@dataclass
class Verdict:
    allowed: bool
    lots: float = 0.0
    reasons: list = field(default_factory=list)


class RiskManager:
    def __init__(self, limits: RiskLimits = RiskLimits(), kill_file: str | None = None):
        self.limits = limits
        self.kill_file = kill_file      # if this file exists, the kill switch is armed
        self._armed = False
        self.arm_reason = None

    # --- kill switch -------------------------------------------------------
    @property
    def armed(self) -> bool:
        if self._armed:
            return True
        if self.kill_file and os.path.exists(self.kill_file):
            self.arm_reason = self.arm_reason or "kill_file"
            return True
        return False

    def arm(self, reason: str):
        self._armed = True
        self.arm_reason = reason

    def disarm(self, acct: AccountState | None = None, reset_peak: bool = False):
        """Manual operator action only. Without reset_peak a drawdown breach re-arms at once."""
        self._armed = False
        self.arm_reason = None
        if acct is not None and reset_peak:
            acct.peak_equity = acct.equity

    # --- account-level checks ---------------------------------------------
    def evaluate_account(self, acct: AccountState):
        if acct.dd_pct >= self.limits.max_drawdown_pct:
            self.arm("max_drawdown")

    def usage(self, acct: AccountState) -> float:
        """Largest share of any limit currently used (0..1+)."""
        L = self.limits
        return max(
            acct.dd_pct / L.max_drawdown_pct,
            acct.day_loss_pct / L.max_daily_loss_pct,
            acct.trades_today / L.max_trades_per_day,
            acct.open_risk_usd / acct.equity * 100.0 / L.max_total_open_risk_pct if acct.equity > 0 else 1.0,
        )

    def risk_state(self, acct: AccountState) -> str:
        if self.armed:
            return "reduce"
        u = self.usage(acct)
        if u >= self.limits.reduce_frac:
            return "reduce"
        if u >= self.limits.near_limit_frac:
            return "near_limit"
        return "safe"

    # --- per-order check ---------------------------------------------------
    def check_order(self, order: Order, acct: AccountState, spec, snap: dict) -> Verdict:
        L = self.limits
        self.evaluate_account(acct)
        if self.armed:
            return Verdict(False, reasons=[f"kill_switch:{self.arm_reason}"])
        if acct.day_loss_pct >= L.max_daily_loss_pct:
            return Verdict(False, reasons=["max_daily_loss"])
        if acct.trades_today >= L.max_trades_per_day:
            return Verdict(False, reasons=["max_trades_per_day"])
        if snap.get("age_s", 0) > L.stale_data_seconds:
            return Verdict(False, reasons=["stale_data"])
        if snap.get("blackout"):
            return Verdict(False, reasons=["event_blackout"])
        if snap.get("spread_x", 1.0) > L.max_spread_mult:
            return Verdict(False, reasons=["spread_too_wide"])
        if order.symbol in acct.positions:
            return Verdict(False, reasons=["position_already_open"])

        reasons = []
        lots = order.lots
        per_lot_risk = order.risk_usd / order.lots if order.lots > 0 else math.inf

        def clamp(new_lots, why):
            nonlocal lots
            new_lots = math.floor(new_lots / spec.lot_step + 1e-9) * spec.lot_step
            if new_lots < lots:
                lots = round(new_lots, 8)
                reasons.append(why)

        clamp(acct.equity * L.max_risk_per_trade_pct / 100.0 / per_lot_risk, "clamped_trade_risk")
        room = acct.equity * L.max_total_open_risk_pct / 100.0 - acct.open_risk_usd
        clamp(max(room, 0.0) / per_lot_risk, "clamped_total_risk")
        clamp(acct.equity * L.max_leverage / (spec.contract_size * order.entry_ref), "clamped_leverage")
        clamp(spec.max_lot, "clamped_max_lot")

        if lots < spec.min_lot:
            return Verdict(False, reasons=reasons + ["below_min_lot_after_limits"])
        return Verdict(True, round(lots, 8), reasons)
