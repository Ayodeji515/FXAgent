"""MT5 Live & Paper Scalping Bot for fxagent.

Connects fxagent's deterministic state engine, ATR sizing, and risk manager
directly to MetaTrader 5 (MT5).

Modes:
  --mode paper   (default) Simulates fills with live tick prices without sending real orders.
  --mode live    Places real market orders with SL, TP, and custom magic number on MT5.

Scalping Timeframes:
  --timeframe M1, M5, M15 (default: M5 for high-probability scalping)

Usage Examples:
  # Paper trade Gold on M5 scalping:
  python mt5_trader.py --symbol XAUUSD --timeframe M5 --mode paper

  # Live trade Gold and Cable with 1% risk per trade:
  python mt5_trader.py --symbol XAUUSD,GBPUSD --timeframe M5 --mode live --risk-pct 1.0
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

# Check for MetaTrader5 package availability
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from fxagent.agent import Agent, AgentAction
from fxagent.config import GateConfig, InstrumentSpec, RiskLimits, StrategyConfig
from fxagent.decider import BaselineDecider
from fxagent.models import AccountState, Order, Position
from fxagent.review import load_active_calibration
from fxagent.risk import RiskManager
from fxagent.state_engine import Bar

TIMEFRAME_MAP = {
    "M1": (1, 60),
    "M5": (5, 300),
    "M15": (15, 900),
    "M30": (30, 1800),
    "H1": (60, 3600),
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
STATUS_FILE = os.path.join(OUT_DIR, "mt5_status.json")


def resolve_mt5_timeframe(tf_str: str):
    if not MT5_AVAILABLE:
        return 5, 300
    tf_map = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
    }
    return tf_map.get(tf_str.upper(), mt5.TIMEFRAME_M5), TIMEFRAME_MAP.get(tf_str.upper(), (5, 300))[1]


def connect_mt5(login: Optional[int] = None, password: Optional[str] = None,
                server: Optional[str] = None, path: Optional[str] = None) -> bool:
    if not MT5_AVAILABLE:
        print("[ERROR] 'MetaTrader5' library is not installed in this Python environment.")
        print("Run: pip install MetaTrader5 (or activate .venv)")
        return False

    init_kwargs = {}
    if path:
        init_kwargs["path"] = path
    if login:
        init_kwargs["login"] = int(login)
    if password:
        init_kwargs["password"] = password
    if server:
        init_kwargs["server"] = server

    if not mt5.initialize(**init_kwargs):
        err = mt5.last_error()
        print(f"[ERROR] MT5 initialize failed: {err}")
        print("Make sure MetaTrader 5 terminal is installed and running on your Windows PC.")
        return False

    terminal = mt5.terminal_info()
    account = mt5.account_info()
    if account is None:
        print("[WARNING] Connected to MT5 terminal, but no trading account is logged in.")
        print("Please log in to your broker demo/live account inside the MT5 terminal window.")
        return False

    print(f"\n==========================================================")
    print(f" [MT5 CONNECTED]")
    print(f" Terminal: {terminal.name} (Build {terminal.build})")
    print(f" Account:  {account.login} | Server: {account.server}")
    print(f" Balance:  ${account.balance:,.2f} | Equity: ${account.equity:,.2f} | Leverage: 1:{account.leverage}")
    print(f" Trade Allowed: {terminal.trade_allowed}")
    print(f"==========================================================\n")
    return True


def get_symbol_spec(symbol: str) -> Optional[InstrumentSpec]:
    info = mt5.symbol_info(symbol)
    if info is None:
        # Check for broker variations like GOLD, XAUUSDm, etc.
        for candidate in [symbol, symbol + "m", symbol + ".a", symbol + ".r", "GOLD"]:
            info = mt5.symbol_info(candidate)
            if info is not None:
                symbol = candidate
                break
    if info is None:
        print(f"[ERROR] Symbol '{symbol}' not found in broker Market Watch.")
        return None

    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    spread = (tick.ask - tick.bid) if tick else (info.spread * info.point)
    typical_spread = max(spread, info.point * 10)

    # Build InstrumentSpec dynamically using actual broker specification
    return InstrumentSpec(
        symbol=symbol,
        contract_size=float(info.trade_contract_size),
        tick=float(info.point),
        lot_step=float(info.volume_step),
        min_lot=float(info.volume_min),
        max_lot=float(info.volume_max),
        typical_spread=round(typical_spread, info.digits),
        slippage=round(info.point * 5, info.digits),
        commission_per_lot=3.5,
    )


class MT5ScalperBot:
    def __init__(self, symbols, timeframe_str="M5", mode="paper", risk_pct=1.0,
                 magic=777101, atr_period=10, mock=False):
        self.symbols = [s.strip().upper() for s in symbols]
        self.timeframe_str = timeframe_str.upper()
        self.mode = mode.lower()
        self.risk_pct = risk_pct
        self.magic = magic
        self.atr_period = atr_period
        self.mock = mock

        self.mt5_tf, self.bar_seconds = resolve_mt5_timeframe(self.timeframe_str)
        self.agents: Dict[str, Agent] = {}
        self.specs: Dict[str, InstrumentSpec] = {}
        self.last_bar_ts: Dict[str, int] = {}
        self.simulated_positions: Dict[str, Position] = {}
        self.active_trades_history = []
        self._mock_prices = {"XAUUSD": 2350.0, "GBPUSD": 1.2850}

        # Scalping strategy configuration (tighter ATR stops & targets)
        self.strat_cfg = StrategyConfig(
            atr_period=self.atr_period,
            atr_stop_mult=1.3,     # Scalping stop-loss (1.3 ATR)
            atr_tp_mult=2.0,       # Scalping take-profit (2.0 ATR -> 1.54 R:R)
        )
        self.risk_limits = RiskLimits(
            max_drawdown_pct=15.0,
            max_daily_loss_pct=3.0,
            max_risk_per_trade_pct=self.risk_pct,
            max_spread_mult=4.0,   # Reject orders during high news volatility spread spikes
        )
        self.gate_cfg = GateConfig(
            min_setup_quality=2,
            min_confidence=0.78,   # High-conviction filter for scalping
            kelly_fraction=0.25,   # Conservative quarter-Kelly
        )

    def initialize_symbols(self) -> bool:
        from fxagent.config import INSTRUMENTS
        calibration = load_active_calibration(os.path.join(BASE_DIR, "params"))
        for sym in self.symbols:
            if self.mock:
                spec = INSTRUMENTS.get(sym, InstrumentSpec(sym, 100.0, 0.01, 0.01, 0.01, 50.0, 0.35, 0.10, 3.5))
            else:
                spec = get_symbol_spec(sym)
                if spec is None:
                    return False
            self.specs[sym] = spec

            decider = BaselineDecider(calibration)
            risk_mgr = RiskManager(self.risk_limits)
            agent = Agent(
                spec=spec,
                decider=decider,
                risk=risk_mgr,
                gate_cfg=self.gate_cfg,
                strat=self.strat_cfg,
                limits=self.risk_limits
            )
            self.agents[sym] = agent

            # Warmup with historical bars so agent is immediately active
            self._warmup(sym)
        return True

    def _warmup(self, symbol: str):
        if self.mock:
            from fxagent.data import synthetic_bars
            print(f"[{symbol}] [MOCK] Generating 60 simulated {self.timeframe_str} bars...")
            syn = synthetic_bars(self.specs[symbol], n=60, seed=42)
            agent = self.agents[symbol]
            for bar in syn:
                try:
                    agent.engine.update(bar, bar.ts)
                except Exception:
                    pass
            last_ts = syn[-1].ts
            self.last_bar_ts[symbol] = last_ts
            snap = agent.engine.snapshot(last_ts)
            print(f"[{symbol}] [MOCK] Engine warmed up: {len(syn)} bars | Engine Ready: {snap.get('ready')}")
            return

        print(f"[{symbol}] Pre-loading historical {self.timeframe_str} bars from MT5...")
        rates = mt5.copy_rates_from_pos(symbol, self.mt5_tf, 1, 60)
        if rates is None or len(rates) < 40:
            print(f"[{symbol}] Warning: fewer than 40 bars available from MT5.")
            return

        agent = self.agents[symbol]
        for r in rates:
            bar = Bar(
                ts=int(r['time']),
                open=float(r['open']),
                high=float(r['high']),
                low=float(r['low']),
                close=float(r['close']),
                volume=float(r['tick_volume']),
                spread=float(r['spread']) * self.specs[symbol].tick if 'spread' in r.dtype.names else 0.0
            )
            try:
                agent.engine.update(bar, bar.ts)
            except Exception:
                pass

        last_ts = int(rates[-1]['time'])
        self.last_bar_ts[symbol] = last_ts
        snap = agent.engine.snapshot(last_ts)
        print(f"[{symbol}] State engine warmed up: {len(rates)} bars | Engine Ready: {snap.get('ready')}")

    def get_live_account(self) -> AccountState:
        if self.mock or not MT5_AVAILABLE:
            return AccountState(equity=10000.0)
        acct = mt5.account_info()
        if acct is None:
            return AccountState(equity=10000.0)
        return AccountState(equity=float(acct.equity))

    def get_open_position(self, symbol: str) -> Optional[Position]:
        if self.mode == "paper":
            return self.simulated_positions.get(symbol)

        # Query real open positions on MT5
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            for p in positions:
                if p.magic == self.magic:
                    direction = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
                    return Position(
                        symbol=symbol,
                        direction=direction,
                        entry=float(p.price_open),
                        lots=float(p.volume),
                        stop=float(p.sl),
                        tp=float(p.tp),
                        entry_ts=int(p.time),
                        risk_usd=float(abs(p.price_open - p.sl) * self.specs[symbol].contract_size * p.volume) if p.sl else 0.0,
                        meta={"ticket": p.ticket}
                    )
        return None

    def execute_enter(self, symbol: str, order: Order, action: AgentAction, tick) -> bool:
        spec = self.specs[symbol]
        digits = 2 if symbol == "XAUUSD" else (5 if symbol == "GBPUSD" else (mt5.symbol_info(symbol).digits if MT5_AVAILABLE else 5))
        is_buy = order.direction > 0
        price = tick.ask if is_buy else tick.bid
        sl = round(order.stop, digits)
        tp = round(order.tp, digits)

        print(f"\n=======================================================")
        print(f" >>> [ORDER SIGNAL] {symbol} {'BUY' if is_buy else 'SELL'} {order.lots} Lots")
        print(f" Entry: {price} | SL: {sl} | TP: {tp} | Reason: {action.reason}")
        print(f" Confidence: {action.decision.confidence:.2f} | Setup Quality: {action.decision.setup_quality}")
        print(f" Mode: {self.mode.upper()}")
        print(f"=======================================================")

        if self.mode == "paper":
            # Paper execution
            self.simulated_positions[symbol] = Position(
                symbol=symbol,
                direction=order.direction,
                entry=price,
                lots=order.lots,
                stop=sl,
                tp=tp,
                entry_ts=int(time.time()),
                risk_usd=order.risk_usd,
                meta={"simulated": True, "regime": action.decision.regime, "confidence": action.decision.confidence}
            )
            print(f"[PAPER] Position filled in paper simulator at {price}")
            return True

        # LIVE EXECUTION VIA MT5
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": order.lots,
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": self.magic,
            "comment": f"fxagent scalper {action.reason[:15]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # Send order
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            # Fallback filling mode if IOC fails
            if res and res.retcode == 10030: # Unsupported filling mode
                req["type_filling"] = mt5.ORDER_FILLING_RETURN
                res = mt5.order_send(req)

        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"[LIVE MT5 SUCCESS] Order #{res.order} opened at {res.price}!")
            return True
        else:
            err = res.comment if res else mt5.last_error()
            print(f"[LIVE MT5 FAILED] Order rejected by broker: {err}")
            return False

    def execute_exit(self, symbol: str, active_pos: Position, reason: str):
        print(f"\n=======================================================")
        print(f" <<< [EXIT SIGNAL] {symbol} Closing position. Reason: {reason}")
        print(f"=======================================================")

        if self.mode == "paper":
            self.simulated_positions.pop(symbol, None)
            print(f"[PAPER] Position closed.")
            return

        ticket = active_pos.meta.get("ticket")
        if not ticket:
            return

        tick = mt5.symbol_info_tick(symbol)
        is_buy = active_pos.direction > 0
        close_price = tick.bid if is_buy else tick.ask
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": symbol,
            "volume": active_pos.lots,
            "type": close_type,
            "price": close_price,
            "deviation": 20,
            "magic": self.magic,
            "comment": f"fxagent exit {reason[:15]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"[LIVE MT5 SUCCESS] Position #{ticket} closed at {res.price}!")
        else:
            print(f"[LIVE MT5 FAILED] Could not close position: {res.comment if res else mt5.last_error()}")

    def update_status_file(self, acct: AccountState):
        os.makedirs(OUT_DIR, exist_ok=True)
        status = {
            "timestamp": int(time.time()),
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "mock": self.mock,
            "timeframe": self.timeframe_str,
            "account": {
                "equity": round(acct.equity, 2),
                "peak_equity": round(acct.peak_equity, 2),
                "drawdown_pct": round(acct.dd_pct, 2),
            },
            "symbols": {}
        }
        for s in self.symbols:
            if self.mock:
                px = self._mock_prices.get(s, 2000.0)
                spread = self.specs[s].typical_spread
                bid = px - spread / 2.0
                ask = px + spread / 2.0
            else:
                tick = mt5.symbol_info_tick(s) if MT5_AVAILABLE else None
                bid = tick.bid if tick else 0.0
                ask = tick.ask if tick else 0.0
            pos = self.get_open_position(s)
            status["symbols"][s] = {
                "bid": bid,
                "ask": ask,
                "spread_points": round((ask - bid) / self.specs[s].tick, 1) if bid else 0.0,
                "in_position": pos is not None,
                "position_lots": pos.lots if pos else 0.0,
                "position_dir": "LONG" if pos and pos.direction > 0 else ("SHORT" if pos else "FLAT")
            }
        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2)
        except Exception:
            pass

    def run_loop(self):
        print(f"\n==========================================================")
        print(f" [FXAGENT SCALPER BOT ACTIVE]")
        print(f" Monitoring symbols: {', '.join(self.symbols)}")
        print(f" Timeframe: {self.timeframe_str} ({self.bar_seconds}s candles)")
        print(f" Mode: {self.mode.upper()} | Risk Per Trade: {self.risk_pct}%")
        print(f" Magic Number: {self.magic}")
        print(f" Status preview written to: out/mt5_status.json")
        print(f" Press Ctrl+C in PowerShell to stop")
        print(f"==========================================================\n")

        heartbeat_timer = 0
        mock_bar_timer = 0
        import random
        while True:
            try:
                now_ts = int(time.time())
                acct = self.get_live_account()

                for symbol in self.symbols:
                    if self.mock:
                        # In mock mode, simulate a new candle every 4 seconds
                        if now_ts - mock_bar_timer >= 4:
                            mock_bar_timer = now_ts
                            spec = self.specs[symbol]
                            curr_px = self._mock_prices.get(symbol, 2000.0)
                            delta = (random.random() - 0.48) * (spec.tick * 80)
                            open_px = curr_px
                            close_px = round(open_px + delta, 5 if symbol == "GBPUSD" else 2)
                            high_px = max(open_px, close_px) + abs(random.random() * spec.tick * 20)
                            low_px = min(open_px, close_px) - abs(random.random() * spec.tick * 20)
                            self._mock_prices[symbol] = close_px

                            candle_ts = now_ts
                            self.last_bar_ts[symbol] = candle_ts
                            bar = Bar(ts=candle_ts, open=open_px, high=high_px, low=low_px, close=close_px,
                                      volume=50.0, spread=spec.typical_spread)
                            active_pos = self.get_open_position(symbol)
                            agent = self.agents[symbol]
                            action: AgentAction = agent.on_bar(bar, now=now_ts, account=acct, position=active_pos)

                            dt_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            print(f"[{dt_str}] [{symbol}] [MOCK M5] Candle: {bar.close} | Action: {action.kind.upper()} ({action.reason})")

                            class MockTick:
                                ask = close_px + spec.typical_spread / 2.0
                                bid = close_px - spec.typical_spread / 2.0
                            if action.kind == "enter" and action.order is not None and active_pos is None:
                                self.execute_enter(symbol, action.order, action, MockTick)
                            elif action.kind == "exit" and active_pos is not None:
                                self.execute_exit(symbol, active_pos, action.reason)
                        continue

                    # 1. Check for closed candle from MT5
                    rates = mt5.copy_rates_from_pos(symbol, self.mt5_tf, 1, 1)
                    if rates is None or len(rates) == 0:
                        continue

                    last_candle = rates[0]
                    candle_ts = int(last_candle['time'])

                    # Check if this candle has already been processed
                    if candle_ts > self.last_bar_ts.get(symbol, 0):
                        self.last_bar_ts[symbol] = candle_ts
                        tick = mt5.symbol_info_tick(symbol)
                        spread_px = (tick.ask - tick.bid) if tick else 0.0

                        bar = Bar(
                            ts=candle_ts,
                            open=float(last_candle['open']),
                            high=float(last_candle['high']),
                            low=float(last_candle['low']),
                            close=float(last_candle['close']),
                            volume=float(last_candle['tick_volume']),
                            spread=spread_px
                        )

                        active_pos = self.get_open_position(symbol)
                        agent = self.agents[symbol]

                        # Agent decides on closed bar
                        action: AgentAction = agent.on_bar(bar, now=now_ts, account=acct, position=active_pos)

                        dt_str = datetime.fromtimestamp(candle_ts, tz=timezone.utc).strftime("%H:%M:%S")
                        print(f"[{dt_str}] [{symbol}] Candle Close: {bar.close} | Action: {action.kind.upper()} ({action.reason})")

                        if action.kind == "enter" and action.order is not None and active_pos is None:
                            self.execute_enter(symbol, action.order, action, tick)
                        elif action.kind == "exit" and active_pos is not None:
                            self.execute_exit(symbol, active_pos, action.reason)

                # Periodic heartbeat & dashboard status update
                if now_ts - heartbeat_timer >= 5:
                    heartbeat_timer = now_ts
                    self.update_status_file(acct)

                time.sleep(1)

            except KeyboardInterrupt:
                print("\n[STOPPING BOT] Operator requested shutdown.")
                break
            except Exception as e:
                print(f"[LOOP ERROR] {e}")
                time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="fxagent MT5 Scalping Bot")
    parser.add_argument("--symbol", default="XAUUSD", help="Symbols comma-separated (e.g. XAUUSD or XAUUSD,GBPUSD)")
    parser.add_argument("--timeframe", default="M5", choices=["M1", "M5", "M15", "M30", "H1"], help="Scalping candle timeframe")
    parser.add_argument("--mode", default="paper", choices=["paper", "live"], help="Execution mode (paper / live)")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="Risk percent per trade (default 1.0 percent)")
    parser.add_argument("--atr-period", type=int, default=10, help="ATR period for stop & target sizing")
    parser.add_argument("--magic", type=int, default=777101, help="Unique magic number for orders")
    parser.add_argument("--mock", action="store_true", help="Run simulated feed without connecting to MT5")
    parser.add_argument("--login", type=int, default=None, help="MT5 login account number")
    parser.add_argument("--password", type=str, default=None, help="MT5 password")
    parser.add_argument("--server", type=str, default=None, help="MT5 broker server name")
    parser.add_argument("--path", type=str, default=None, help="Path to terminal64.exe if non-standard")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbol.split(",")]

    if not args.mock:
        print("\n--- Initializing MetaTrader 5 Connection ---")
        if not connect_mt5(login=args.login, password=args.password, server=args.server, path=args.path):
            print("\nTip: If you do not have MetaTrader 5 open yet, you can test with: python mt5_trader.py --mock")
            sys.exit(1)
    else:
        print("\n--- Running in MOCK mode (Internal Scalping Simulation) ---")

    bot = MT5ScalperBot(
        symbols=symbols,
        timeframe_str=args.timeframe,
        mode=args.mode,
        risk_pct=args.risk_pct,
        magic=args.magic,
        atr_period=args.atr_period,
        mock=args.mock,
    )

    if not bot.initialize_symbols():
        print("[ERROR] Failed to initialize symbols. Exiting.")
        sys.exit(1)

    bot.run_loop()


if __name__ == "__main__":
    main()
