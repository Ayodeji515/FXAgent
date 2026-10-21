import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fxagent.agent import Agent, AgentAction
from fxagent.backtest import run_backtest
from fxagent.config import INSTRUMENTS, GateConfig, RiskLimits
from fxagent.data import synthetic_bars
from fxagent.decider import BaselineDecider, CallableDecider
from fxagent.models import AccountState, Order, Position
from fxagent.review import (approve_latest, brier_score, fit_calibration,
                            load_active_calibration, propose)
from fxagent.risk import RiskManager
from fxagent.schema import Decision, SchemaError, gate
from fxagent.sizing import build_order, kelly
from fxagent.state_engine import Bar, CausalityError, StateEngine, approx_tokens

XAU = INSTRUMENTS["XAUUSD"]
GBP = INSTRUMENTS["GBPUSD"]
OK_SNAP = {"age_s": 0, "blackout": False, "spread_x": 1.0}


def dec(**kw):
    base = dict(regime="trending", direction="long", setup_quality=3, risk_state="safe",
                confidence=0.9, p_win=0.54)
    base.update(kw)
    return Decision(**base)


def flat_bars(n, price=2000.0, start=1_700_000_000, step=3600, spread=0.35):
    return [Bar(start + (i + 1) * step, price, price, price, price, 0.0, spread) for i in range(n)]


class StateEngineTests(unittest.TestCase):
    def test_rejects_future_and_out_of_order_bars(self):
        e = StateEngine(XAU)
        b = flat_bars(2)
        e.update(b[0], now=b[0].ts)
        with self.assertRaises(CausalityError):
            e.update(b[1], now=b[1].ts - 1)          # bar closes after "now"
        e.update(b[1], now=b[1].ts)
        with self.assertRaises(CausalityError):
            e.update(b[1], now=b[1].ts + 10)         # duplicate / non-increasing

    def test_snapshot_before_last_bar_is_refused(self):
        e = StateEngine(XAU)
        bars = synthetic_bars(XAU, n=60, seed=3)
        for b in bars:
            e.update(b, now=b.ts)
        with self.assertRaises(CausalityError):
            e.snapshot(bars[-1].ts - 1)

    def test_not_ready_until_enough_history(self):
        e = StateEngine(XAU)
        bars = synthetic_bars(XAU, n=39, seed=3)
        for b in bars:
            e.update(b, now=b.ts)
        self.assertFalse(e.snapshot(bars[-1].ts)["ready"])

    def test_snapshot_is_compact_and_deterministic(self):
        bars = synthetic_bars(XAU, n=150, seed=5)
        snaps = []
        for _ in range(2):
            e = StateEngine(XAU, blackouts=[(bars[-1].ts - 10, bars[-1].ts + 10)])
            for b in bars:
                e.update(b, now=b.ts)
            snaps.append(e.snapshot(bars[-1].ts, {"pos_lots": 0.0, "dd_pct": 0.0, "day_loss_pct": 0.0}))
        self.assertEqual(snaps[0], snaps[1])
        self.assertTrue(snaps[0]["ready"])
        self.assertTrue(snaps[0]["blackout"])
        self.assertLess(approx_tokens(snaps[0]), 400)


class SchemaAndGateTests(unittest.TestCase):
    def test_invalid_fields_raise(self):
        for bad in (dict(regime="sideways"), dict(direction="up"), dict(setup_quality=4),
                    dict(setup_quality=True), dict(confidence=1.2), dict(p_win=float("nan")),
                    dict(risk_state="ok")):
            with self.assertRaises(SchemaError):
                dec(**bad)
        with self.assertRaises(SchemaError):
            Decision.from_dict({"regime": "trending"})

    def test_gate_thresholds(self):
        cfg = GateConfig()
        self.assertEqual(gate(dec(), "safe", cfg), (True, "ok"))
        self.assertEqual(gate(dec(confidence=0.80), "safe", cfg)[1], "low_confidence")   # strictly greater
        self.assertEqual(gate(dec(setup_quality=1), "safe", cfg)[1], "low_setup_quality")
        self.assertEqual(gate(dec(regime="crisis"), "safe", cfg)[1], "crisis_regime")
        self.assertEqual(gate(dec(direction="neutral"), "safe", cfg)[1], "neutral")

    def test_code_risk_state_overrides_model(self):
        cfg = GateConfig()
        self.assertEqual(gate(dec(risk_state="safe"), "near_limit", cfg)[1], "risk_state_not_safe")
        self.assertEqual(gate(dec(risk_state="reduce"), "safe", cfg)[1], "risk_state_not_safe")


class SizingTests(unittest.TestCase):
    SNAP = {"atr": 5.0, "spread": 0.35, "mid": 2000.0}

    def test_kelly(self):
        self.assertAlmostEqual(kelly(0.5, 1.0), 0.0)
        self.assertAlmostEqual(kelly(0.6, 1.0), 0.2)

    def test_order_is_capped_at_one_percent_risk(self):
        order, why = build_order(dec(), self.SNAP, XAU, 10_000.0)
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(order.lots, 0.12)
        self.assertLessEqual(order.risk_usd, 100.0 + 1e-6)
        self.assertAlmostEqual(order.stop, 2000.0 - 7.5)
        self.assertAlmostEqual(order.tp, 2000.0 + 11.25)

    def test_short_orders_mirror(self):
        order, _ = build_order(dec(direction="short"), self.SNAP, XAU, 10_000.0)
        self.assertEqual(order.direction, -1)
        self.assertAlmostEqual(order.stop, 2007.5)
        self.assertAlmostEqual(order.tp, 1988.75)

    def test_no_edge_and_cost_dominated_setups_are_skipped(self):
        self.assertEqual(build_order(dec(p_win=0.30), self.SNAP, XAU, 10_000.0), (None, "no_edge_after_costs"))
        tiny = {"atr": 0.1, "spread": 0.35, "mid": 2000.0}
        self.assertEqual(build_order(dec(), tiny, XAU, 10_000.0), (None, "costs_exceed_target"))

    def test_below_min_lot(self):
        self.assertEqual(build_order(dec(), self.SNAP, XAU, 500.0), (None, "below_min_lot"))


class RiskTests(unittest.TestCase):
    ORDER = Order("XAUUSD", 1, 0.5, 2000.0, 1990.0, 2020.0, risk_usd=500.0)   # 1000 USD per lot

    def test_drawdown_arms_kill_switch_and_stays_armed(self):
        rm = RiskManager()
        acct = AccountState(equity=10_000.0)
        acct.mark(10_000.0, 0)
        acct.mark(8_400.0, 3600)                    # 16% drawdown
        v = rm.check_order(self.ORDER, acct, XAU, OK_SNAP)
        self.assertFalse(v.allowed)
        self.assertTrue(rm.armed)
        acct.mark(10_000.0, 7200)                   # recovery does not re-enable trading
        self.assertFalse(rm.check_order(self.ORDER, acct, XAU, OK_SNAP).allowed)
        self.assertEqual(rm.risk_state(acct), "reduce")
        rm.disarm(acct, reset_peak=True)
        self.assertTrue(rm.check_order(self.ORDER, acct, XAU, OK_SNAP).allowed)

    def test_kill_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "KILL")
            rm = RiskManager(kill_file=path)
            acct = AccountState(equity=10_000.0)
            self.assertTrue(rm.check_order(self.ORDER, acct, XAU, OK_SNAP).allowed)
            open(path, "w").close()
            self.assertFalse(rm.check_order(self.ORDER, acct, XAU, OK_SNAP).allowed)

    def test_daily_loss_blocks_then_resets_next_day(self):
        rm = RiskManager()
        acct = AccountState(equity=10_000.0)
        acct.mark(10_000.0, 0)
        acct.mark(9_600.0, 3600)                    # -4% same UTC day
        self.assertEqual(rm.check_order(self.ORDER, acct, XAU, OK_SNAP).reasons, ["max_daily_loss"])
        acct.mark(9_600.0, 86_400 + 60)             # next UTC day: baseline resets
        self.assertTrue(rm.check_order(self.ORDER, acct, XAU, OK_SNAP).allowed)

    def test_market_condition_vetoes(self):
        rm, acct = RiskManager(), AccountState(equity=10_000.0)
        for snap, reason in (({"age_s": 99_999, "blackout": False, "spread_x": 1.0}, "stale_data"),
                             ({"age_s": 0, "blackout": True, "spread_x": 1.0}, "event_blackout"),
                             ({"age_s": 0, "blackout": False, "spread_x": 5.0}, "spread_too_wide")):
            self.assertEqual(rm.check_order(self.ORDER, acct, XAU, snap).reasons, [reason])

    def test_size_is_clamped_not_trusted(self):
        rm, acct = RiskManager(), AccountState(equity=10_000.0)
        v = rm.check_order(self.ORDER, acct, XAU, OK_SNAP)
        self.assertTrue(v.allowed)
        self.assertAlmostEqual(v.lots, 0.10)        # 1% of 10k at 1000 USD/lot
        self.assertIn("clamped_trade_risk", v.reasons)

    def test_total_open_risk_cap(self):
        rm, acct = RiskManager(), AccountState(equity=10_000.0)
        acct.positions["GBPUSD"] = Position("GBPUSD", 1, 1.0, 1.3, 1.29, 1.32, 0, risk_usd=150.0)
        v = rm.check_order(self.ORDER, acct, XAU, OK_SNAP)
        self.assertAlmostEqual(v.lots, 0.05)        # only 50 USD of the 200 USD cap remains
        self.assertIn("clamped_total_risk", v.reasons)

    def test_one_position_per_symbol(self):
        rm, acct = RiskManager(), AccountState(equity=10_000.0)
        acct.positions["XAUUSD"] = Position("XAUUSD", 1, 0.1, 2000, 1990, 2020, 0, 100.0)
        self.assertEqual(rm.check_order(self.ORDER, acct, XAU, OK_SNAP).reasons, ["position_already_open"])

    def test_risk_state_thresholds(self):
        rm, acct = RiskManager(), AccountState(equity=10_000.0)
        acct.mark(10_000.0, 0)
        self.assertEqual(rm.risk_state(acct), "safe")
        acct.mark(9_800.0, 60)                      # 2% day loss = 67% of 3% limit
        self.assertEqual(rm.risk_state(acct), "near_limit")
        acct.mark(9_700.0, 120)                     # 3% day loss = 100% of limit
        self.assertEqual(rm.risk_state(acct), "reduce")


class DeciderTests(unittest.TestCase):
    SNAP = {"ready": True, "x": 1}

    def test_model_failures_collapse_to_hold(self):
        def boom(_):
            raise TimeoutError("model timed out")
        d = CallableDecider(boom)
        out = d.decide(self.SNAP)
        self.assertEqual((out.direction, out.confidence), ("neutral", 0.0))
        self.assertEqual(d.failures, 1)
        self.assertEqual(CallableDecider(lambda s: {"regime": "trending"}).decide(self.SNAP).direction, "neutral")
        self.assertEqual(CallableDecider(lambda s: "not json").decide(self.SNAP).direction, "neutral")

    def test_valid_model_output_passes_through(self):
        good = dict(regime="trending", direction="long", setup_quality=3, risk_state="safe",
                    confidence=0.9, p_win=0.55)
        self.assertEqual(CallableDecider(lambda s: good).decide(self.SNAP).direction, "long")

    def test_baseline_holds_when_not_ready(self):
        self.assertEqual(BaselineDecider().decide({"ready": False}).direction, "neutral")


class StubDecider:
    def __init__(self, decision):
        self.decision = decision

    def decide(self, snap):
        return self.decision


def warmed_agent(decider, deep=None, risk=None):
    agent = Agent(XAU, decider, risk or RiskManager(), deep_decider=deep)
    bars = synthetic_bars(XAU, n=60, seed=9)
    acct = AccountState(equity=10_000.0)
    for b in bars[:-1]:
        agent.on_bar(b, b.ts, acct, None)
    return agent, bars[-1], acct


POS = Position("XAUUSD", 1, 0.1, 2000.0, 1990.0, 2020.0, 0, 100.0)


class AgentTests(unittest.TestCase):
    def test_crisis_without_deep_model_flattens(self):
        agent, last, acct = warmed_agent(StubDecider(dec(regime="crisis", confidence=0.9)))
        a = agent.on_bar(last, last.ts, acct, POS)
        self.assertEqual((a.kind, a.reason), ("exit", "crisis_flatten"))

    def test_low_confidence_without_deep_model_holds_and_lets_stops_work(self):
        agent, last, acct = warmed_agent(StubDecider(dec(confidence=0.4)))
        a = agent.on_bar(last, last.ts, acct, POS)
        self.assertEqual((a.kind, a.reason), ("none", "hold"))

    def test_deep_model_can_only_tighten(self):
        fast = StubDecider(dec(confidence=0.4))
        agent, last, acct = warmed_agent(fast, deep=StubDecider(dec(direction="short", confidence=0.9)))
        a = agent.on_bar(last, last.ts, acct, POS)
        self.assertEqual((a.kind, a.reason, a.escalated), ("exit", "deep_model_exit", True))
        agent, last, acct = warmed_agent(fast, deep=StubDecider(dec(direction="long", confidence=0.9)))
        a = agent.on_bar(last, last.ts, acct, POS)
        self.assertEqual((a.kind, a.reason), ("none", "deep_model_hold"))

    def test_kill_switch_flattens_and_blocks_entries(self):
        risk = RiskManager()
        agent, last, acct = warmed_agent(StubDecider(dec()), risk=risk)
        risk.arm("test")
        self.assertEqual(agent.on_bar(last, last.ts, acct, POS).kind, "exit")

    def test_entry_when_everything_passes(self):
        agent, last, acct = warmed_agent(StubDecider(dec()))
        a = agent.on_bar(last, last.ts, acct, None)
        self.assertEqual(a.kind, "enter")
        self.assertLessEqual(a.order.risk_usd, 10_000.0 * 0.01 + 1e-6)

    def test_entry_blocked_inside_event_blackout(self):
        bars = synthetic_bars(XAU, n=60, seed=9)
        agent = Agent(XAU, StubDecider(dec()), RiskManager(), blackouts=[(bars[-1].ts - 60, bars[-1].ts + 60)])
        acct = AccountState(equity=10_000.0)
        for b in bars[:-1]:
            agent.on_bar(b, b.ts, acct, None)
        a = agent.on_bar(bars[-1], bars[-1].ts, acct, None)
        self.assertEqual(a.kind, "none")
        self.assertIn("event_blackout", a.reason)


class ScriptedAgent:
    """Issues pre-scripted actions by bar index to test the simulator's fill logic."""

    def __init__(self, enter_at, exit_at=None, order=None):
        self.risk = RiskManager()
        self.i = -1
        self.enter_at, self.exit_at = enter_at, exit_at
        self.order = order or Order("XAUUSD", 1, 0.1, 2000.0, 1990.0, 2020.0, risk_usd=100.0)
        self.decision = dec()

    def on_bar(self, bar, now, account, position):
        self.i += 1
        snap = {"ready": True}
        if self.i == self.enter_at and position is None:
            return AgentAction("enter", "ok", self.order, self.decision, snapshot=snap)
        if self.exit_at is not None and self.i == self.exit_at and position is not None:
            return AgentAction("exit", "scripted", decision=self.decision, snapshot=snap)
        return AgentAction("none", "x", snapshot=snap)


class BacktestTests(unittest.TestCase):
    def test_entry_fills_on_next_open_with_costs(self):
        bars = flat_bars(12)
        res = run_backtest(XAU, bars, ScriptedAgent(enter_at=5))
        t = res.trades[0]
        self.assertEqual(t.entry_ts, bars[6].ts)                       # next bar, never the decision bar
        self.assertAlmostEqual(t.entry, 2000.0 + 0.175 + 0.10)         # half spread + slippage
        self.assertEqual(t.reason, "end_of_data")

    def test_round_trip_cost_on_flat_market(self):
        bars = flat_bars(14)
        res = run_backtest(XAU, bars, ScriptedAgent(enter_at=5, exit_at=8))
        t = res.trades[0]
        self.assertEqual(t.exit_ts, bars[9].ts)
        self.assertAlmostEqual(t.exit, 2000.0 - 0.175 - 0.10)
        # price change -0.55 * 100 oz * 0.1 lots = -5.5, plus 2 * 3.5 * 0.1 commission = 0.7
        self.assertAlmostEqual(t.pnl, -6.2, places=6)

    def test_stop_wins_when_stop_and_target_share_a_bar(self):
        bars = flat_bars(12)
        bars[7] = Bar(bars[7].ts, 2000.0, 2025.0, 1985.0, 2000.0, 0.0, 0.35)
        res = run_backtest(XAU, bars, ScriptedAgent(enter_at=5))
        self.assertEqual(res.trades[0].reason, "stop")
        self.assertLess(res.trades[0].pnl, 0)

    def test_target_fill(self):
        bars = flat_bars(12)
        bars[7] = Bar(bars[7].ts, 2000.0, 2021.0, 1999.0, 2020.5, 0.0, 0.35)
        res = run_backtest(XAU, bars, ScriptedAgent(enter_at=5))
        self.assertEqual((res.trades[0].reason, res.trades[0].exit), ("target", 2020.0))

    def test_gap_through_stop_fills_at_gapped_price(self):
        bars = flat_bars(12)
        bars[7] = Bar(bars[7].ts, 1970.0, 1970.0, 1970.0, 1970.0, 0.0, 0.35)
        res = run_backtest(XAU, bars, ScriptedAgent(enter_at=5))
        t = res.trades[0]
        self.assertEqual(t.reason, "stop")
        self.assertLess(t.exit, 1971.0)                                # far worse than the 1990 stop

    def test_zero_edge_data_is_survivable_and_bounded(self):
        for spec in (XAU, GBP):
            bars = synthetic_bars(spec, n=6000, seed=11)
            agent = Agent(spec, BaselineDecider(), RiskManager(RiskLimits()))
            res = run_backtest(spec, bars, agent, equity0=10_000.0)
            m = res.metrics(10_000.0)
            self.assertLess(m["max_drawdown_pct"], 18.0)               # 15% kill + at most one trade of overshoot
            self.assertTrue(all(t.r > -2.5 for t in res.trades))       # no trade loses wildly more than planned
            self.assertTrue(all(eq > 0 for _, eq in res.equity_curve))


class ReviewTests(unittest.TestCase):
    def test_brier(self):
        self.assertAlmostEqual(brier_score([1.0, 0.0], [1, 0]), 0.0)
        self.assertAlmostEqual(brier_score([0.5, 0.5], [1, 0]), 0.25)

    def test_fit_refuses_thin_data_and_shrinks_overconfidence(self):
        self.assertIsNone(fit_calibration([0.9] * 10, [1, 0] * 5))
        ps, ys = [0.9] * 60, [1, 0] * 30
        fit = fit_calibration(ps, ys)
        self.assertLess(fit["brier_after"], brier_score(ps, ys))
        self.assertLess(fit["alpha"] * 0.4 + fit["bias"], 0.4)         # 0.9 pulled toward 0.5

    def test_proposals_need_operator_approval(self):
        with tempfile.TemporaryDirectory() as d:
            trades = os.path.join(d, "trades.jsonl")
            with open(trades, "w") as f:
                for i in range(40):
                    f.write(json.dumps({"p_win": 0.8, "pnl": 10.0 if i % 2 else -10.0, "r": 0.1}) + "\n")
            out = propose(trades, os.path.join(d, "params"))
            self.assertEqual((out["status"], out["version"]), ("proposed", 1))
            self.assertIsNone(load_active_calibration(os.path.join(d, "params")))   # not live yet
            approve_latest(os.path.join(d, "params"))
            self.assertIn("alpha", load_active_calibration(os.path.join(d, "params")))

    def test_propose_reports_insufficient_data(self):
        with tempfile.TemporaryDirectory() as d:
            trades = os.path.join(d, "trades.jsonl")
            with open(trades, "w") as f:
                f.write(json.dumps({"p_win": 0.6, "pnl": 1.0, "r": 0.1}) + "\n")
            self.assertEqual(propose(trades, os.path.join(d, "params"))["status"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()
