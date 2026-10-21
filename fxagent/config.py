"""All thresholds live here, in code. The decision model never sets any of these."""

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    contract_size: float        # units per 1.0 lot (both pairs are USD-quoted, so P&L is in USD)
    tick: float
    lot_step: float
    min_lot: float
    max_lot: float
    typical_spread: float       # price units; REPLACE with your broker's real average
    slippage: float             # modeled price units per market fill; REPLACE after measuring
    commission_per_lot: float   # USD per lot per side; REPLACE with your broker's schedule


# Placeholder microstructure numbers. Measure your own broker's spreads, slippage
# and commissions before trusting any backtest result.
INSTRUMENTS = {
    "XAUUSD": InstrumentSpec("XAUUSD", 100.0, 0.01, 0.01, 0.01, 50.0, 0.35, 0.10, 3.5),
    "GBPUSD": InstrumentSpec("GBPUSD", 100_000.0, 0.00001, 0.01, 0.01, 50.0, 0.00012, 0.00003, 3.5),
}


@dataclass(frozen=True)
class RiskLimits:
    max_drawdown_pct: float = 15.0        # from peak equity; breach arms the kill switch
    max_daily_loss_pct: float = 3.0       # from start-of-day equity; blocks new entries
    max_risk_per_trade_pct: float = 1.0   # stop-loss distance risk, spread included
    max_total_open_risk_pct: float = 2.0
    max_leverage: float = 10.0            # notional / equity
    max_trades_per_day: int = 6
    max_spread_mult: float = 3.0          # reject entries when spread > mult * typical
    stale_data_seconds: int = 7200        # reject entries when the last bar is older than this
    near_limit_frac: float = 0.60         # share of any limit used -> risk_state "near_limit"
    reduce_frac: float = 0.85             # share of any limit used -> risk_state "reduce"


@dataclass(frozen=True)
class GateConfig:
    min_setup_quality: int = 2            # 0..3
    min_confidence: float = 0.80          # strictly greater than
    kelly_fraction: float = 0.25          # quarter Kelly
    escalate_confidence: float = 0.60     # below this (or crisis) -> deep re-read / flatten


@dataclass(frozen=True)
class StrategyConfig:
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    atr_tp_mult: float = 2.25             # reward:risk of 1.5 before costs
