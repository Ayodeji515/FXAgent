"""fxagent: a risk-first autonomous trading system skeleton for XAUUSD and GBPUSD.

Layers:
  state_engine  deterministic numeric snapshots, strictly causal
  decider       pluggable fast decision model (any model that returns the schema)
  schema        typed decision schema + validation
  sizing        fractional Kelly sizing and ATR-based stops, all in code
  risk          hard limits and kill switch that no model output can override
  agent         glue: snapshot -> decision -> gate -> size -> risk check
  backtest      cost-aware, next-bar-fill simulator
  review        nightly calibration review (Brier score), operator-approved

This is research and engineering scaffolding. Nothing here is a proven edge.
"""

__version__ = "0.1.0"
