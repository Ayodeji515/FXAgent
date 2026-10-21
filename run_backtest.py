"""Run the full pipeline on synthetic or CSV data.

  python3 run_backtest.py                       # synthetic, zero-edge data (sanity check)
  python3 run_backtest.py --csv XAUUSD=xau.csv --csv GBPUSD=gbp.csv

Synthetic results say nothing about real markets. With zero edge you should see roughly
cost-sized losses, bounded by the risk layer. Real data is the only meaningful test.
"""

import argparse
import json
import os
from dataclasses import asdict

from fxagent.agent import Agent
from fxagent.backtest import run_backtest, write_jsonl
from fxagent.config import INSTRUMENTS, RiskLimits
from fxagent.data import load_csv, synthetic_bars
from fxagent.decider import BaselineDecider
from fxagent.review import load_active_calibration
from fxagent.risk import RiskManager


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="append", default=[], help="SYMBOL=path.csv (repeatable)")
    ap.add_argument("--bars", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--momentum", type=float, default=0.0, help="synthetic autocorrelation; 0 = no edge")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--out", default="out")
    ap.add_argument("--params-dir", default="params")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    csvs = dict(c.split("=", 1) for c in args.csv)
    calibration = load_active_calibration(args.params_dir)
    all_trades, summary = [], {}
    for symbol, spec in INSTRUMENTS.items():
        bars = (load_csv(csvs[symbol]) if symbol in csvs
                else synthetic_bars(spec, n=args.bars, seed=args.seed, momentum=args.momentum))
        agent = Agent(spec, BaselineDecider(calibration), RiskManager(RiskLimits()))
        res = run_backtest(spec, bars, agent, equity0=args.equity)
        summary[symbol] = res.metrics(args.equity)
        all_trades += [asdict(t) for t in res.trades]
        write_jsonl(os.path.join(args.out, f"decisions_{symbol}.jsonl"), res.decisions)
    write_jsonl(os.path.join(args.out, "trades.jsonl"), all_trades)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
