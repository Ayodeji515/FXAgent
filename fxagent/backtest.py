"""Cost-aware, no-lookahead backtester.

Timing: the agent decides at a bar's CLOSE; entries and discretionary exits fill at the NEXT
bar's OPEN. Stops and targets trigger intrabar against bid/ask-adjusted high/low. If a stop and
a target are both reachable in one bar, the stop is assumed to hit first (conservative).
Costs: half-spread each side, fixed slippage on market and stop fills, commission per lot per side.
Not modeled: swaps/rollover, requotes, partial fills, weekend gaps beyond what the bars contain.
"""

import json
import math
from dataclasses import asdict, dataclass, field

from .models import AccountState, Position


@dataclass
class Trade:
    symbol: str
    direction: int
    lots: float
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    pnl: float
    r: float
    reason: str
    p_win: float
    confidence: float
    regime: str


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    killed: bool = False
    kill_reason: str | None = None

    def metrics(self, start_equity: float) -> dict:
        pnl = [t.pnl for t in self.trades]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x <= 0]
        peak, mdd = start_equity, 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak * 100.0)
        end = self.equity_curve[-1][1] if self.equity_curve else start_equity
        return {
            "trades": len(pnl),
            "win_rate": round(len(wins) / len(pnl), 3) if pnl else None,
            "net_pnl": round(sum(pnl), 2),
            "return_pct": round((end / start_equity - 1) * 100, 2),
            "max_drawdown_pct": round(mdd, 2),
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "expectancy_r": round(sum(t.r for t in self.trades) / len(self.trades), 3) if self.trades else None,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
        }


def _half_spread(bar, spec):
    return (bar.spread if bar.spread > 0 else spec.typical_spread) / 2.0


def run_backtest(spec, bars, agent, equity0: float = 10_000.0) -> BacktestResult:
    res = BacktestResult()
    acct = AccountState(equity=equity0)
    balance = equity0
    pos: Position | None = None
    pending_entry = None            # (Order, decision)
    pending_exit = None             # reason

    def close_position(exit_price, ts, reason):
        nonlocal pos, balance
        gross = pos.direction * (exit_price - pos.entry) * spec.contract_size * pos.lots
        comm = 2 * spec.commission_per_lot * pos.lots
        balance += gross - spec.commission_per_lot * pos.lots   # entry commission already taken
        pnl = gross - comm
        res.trades.append(Trade(
            symbol=spec.symbol, direction=pos.direction, lots=pos.lots, entry_ts=pos.entry_ts,
            exit_ts=ts, entry=pos.entry, exit=exit_price, pnl=pnl,
            r=pnl / pos.risk_usd if pos.risk_usd else 0.0, reason=reason,
            p_win=pos.meta.get("p_win", 0.0), confidence=pos.meta.get("confidence", 0.0),
            regime=pos.meta.get("regime", "")))
        acct.positions.pop(spec.symbol, None)
        pos = None

    for bar in bars:
        hs = _half_spread(bar, spec)

        # A. fills at this bar's open (decisions were made on the previous close)
        if pending_exit and pos is not None:
            px = bar.open - pos.direction * (hs + spec.slippage)
            close_position(px, bar.ts, pending_exit)
        pending_exit = None
        if pending_entry is not None and pos is None:
            order, decision = pending_entry
            fill = bar.open + order.direction * (hs + spec.slippage)
            pos = Position(spec.symbol, order.direction, order.lots, fill, order.stop, order.tp,
                           bar.ts, order.risk_usd,
                           meta={"p_win": decision.p_win, "confidence": decision.confidence,
                                 "regime": decision.regime})
            balance -= spec.commission_per_lot * order.lots
            acct.positions[spec.symbol] = pos
            acct.trades_today += 1
        pending_entry = None

        # B. intrabar stop / target (bid/ask adjusted; stop wins ties)
        if pos is not None:
            d = pos.direction
            if d > 0:
                stop_hit = bar.low - hs <= pos.stop
                tp_hit = bar.high - hs >= pos.tp
            else:
                stop_hit = bar.high + hs >= pos.stop
                tp_hit = bar.low + hs <= pos.tp
            if stop_hit:
                # Stop is on the bid (long) / ask (short) side; a gap through it fills at the gapped price.
                level = min(pos.stop, bar.open - hs) if d > 0 else max(pos.stop, bar.open + hs)
                close_position(level - d * spec.slippage, bar.ts, "stop")
            elif tp_hit:
                close_position(pos.tp, bar.ts, "target")

        # C. mark to market on the close (liquidation-price estimate)
        unreal = 0.0
        if pos is not None:
            liq = bar.close - pos.direction * hs
            unreal = pos.direction * (liq - pos.entry) * spec.contract_size * pos.lots
        acct.mark(balance + unreal, bar.ts)
        res.equity_curve.append((bar.ts, acct.equity))

        # D. decide on this close
        action = agent.on_bar(bar, bar.ts, acct, pos)
        if action.snapshot and action.snapshot.get("ready"):
            res.decisions.append({
                "ts": bar.ts, "kind": action.kind, "reason": action.reason,
                "decision": asdict(action.decision) if action.decision else None})
        if action.kind == "enter":
            pending_entry = (action.order, action.decision)
        elif action.kind == "exit" and pos is not None:
            pending_exit = action.reason

    if pos is not None:
        last = bars[-1]
        close_position(last.close - pos.direction * _half_spread(last, spec), last.ts, "end_of_data")
        acct.mark(balance, last.ts)
        res.equity_curve[-1] = (last.ts, acct.equity)

    res.killed = agent.risk.armed
    res.kill_reason = agent.risk.arm_reason
    return res


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
