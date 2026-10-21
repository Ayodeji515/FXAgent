"""Interactive localhost web dashboard for fxagent.
Zero-dependency: uses Python standard library http.server.
Run with:
    python dashboard.py
Then open http://localhost:5050 in your browser.
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 5050
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
PARAMS_DIR = os.path.join(BASE_DIR, "params")


def load_trades():
    trades_path = os.path.join(OUT_DIR, "trades.jsonl")
    if not os.path.exists(trades_path):
        return []
    trades = []
    with open(trades_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades


def load_decisions(symbol):
    path = os.path.join(OUT_DIR, f"decisions_{symbol}.jsonl")
    if not os.path.exists(path):
        return {"reasons": {}, "total": 0, "sample": []}
    reasons = {}
    total = 0
    sample = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            total += 1
            d = json.loads(line)
            r = d.get("reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
            if len(sample) < 15:
                sample.append(d)
    return {"reasons": reasons, "total": total, "sample": sample}


def compute_metrics(trades, equity0=10000.0):
    if not trades:
        return {
            "trades": 0,
            "win_rate": 0,
            "net_pnl": 0.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "profit_factor": 0.0,
            "expectancy_r": 0.0,
            "wins": 0,
            "losses": 0,
            "curve": [{"time": 0, "equity": equity0, "pnl": 0.0}],
        }
    pnl_list = [t["pnl"] for t in trades]
    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]
    eq = equity0
    peak = equity0
    mdd = 0.0
    curve = [{"time": trades[0]["entry_ts"], "equity": equity0, "pnl": 0.0}]
    cum_pnl = 0.0
    for t in trades:
        cum_pnl += t["pnl"]
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        mdd = max(mdd, dd)
        curve.append({"time": t["exit_ts"], "equity": round(eq, 2), "pnl": round(cum_pnl, 2)})

    win_rate = round(len(wins) / len(pnl_list) * 100, 1) if pnl_list else 0.0
    pf = round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else (99.0 if wins else 0.0)
    exp_r = round(sum(t.get("r", 0.0) for t in trades) / len(trades), 3) if trades else 0.0

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "net_pnl": round(sum(pnl_list), 2),
        "return_pct": round((eq / equity0 - 1) * 100, 2),
        "max_drawdown_pct": round(mdd, 2),
        "profit_factor": pf,
        "expectancy_r": exp_r,
        "wins": len(wins),
        "losses": len(losses),
        "curve": curve,
    }


def load_mt5_status():
    status_path = os.path.join(OUT_DIR, "mt5_status.json")
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def get_dashboard_data():
    trades = load_trades()
    xau_trades = [t for t in trades if t.get("symbol") == "XAUUSD"]
    gbp_trades = [t for t in trades if t.get("symbol") == "GBPUSD"]

    return {
        "overall": compute_metrics(trades),
        "xau": compute_metrics(xau_trades),
        "gbp": compute_metrics(gbp_trades),
        "trades": trades[-50:],  # last 50 trades
        "total_trades_count": len(trades),
        "decisions_xau": load_decisions("XAUUSD"),
        "decisions_gbp": load_decisions("GBPUSD"),
        "mt5_status": load_mt5_status(),
    }


HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FXAgent - Autonomous Trading Intelligence & Risk Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #0a0d14;
      --bg-surface: #111726;
      --bg-card: #151d30;
      --bg-card-hover: #1c2640;
      --border: rgba(255, 255, 255, 0.08);
      --border-accent: rgba(99, 102, 241, 0.35);
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --accent-indigo: #6366f1;
      --accent-cyan: #06b6d4;
      --accent-emerald: #10b981;
      --accent-rose: #f43f5e;
      --accent-amber: #f59e0b;
      --accent-purple: #a855f7;
      --glow-indigo: rgba(99, 102, 241, 0.15);
      --glow-emerald: rgba(16, 185, 129, 0.15);
      --glow-rose: rgba(244, 63, 94, 0.15);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Outfit', sans-serif;
      background-color: var(--bg-base);
      color: var(--text-main);
      min-height: 100vh;
      overflow-x: hidden;
      background-image: 
        radial-gradient(ellipse 80% 50% at 50% -20%, rgba(99, 102, 241, 0.12), transparent),
        radial-gradient(ellipse 60% 40% at 100% 30%, rgba(6, 182, 212, 0.08), transparent);
    }

    /* Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 1.25rem 2rem;
      background: rgba(17, 23, 38, 0.7);
      backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 0.85rem;
    }
    .brand-icon {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: linear-gradient(135deg, var(--accent-indigo), var(--accent-cyan));
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 1.15rem;
      color: #fff;
      box-shadow: 0 0 20px rgba(99, 102, 241, 0.4);
    }
    .brand-name {
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      background: linear-gradient(to right, #fff, #cbd5e1);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .brand-tag {
      font-size: 0.7rem;
      padding: 0.2rem 0.5rem;
      background: rgba(99, 102, 241, 0.15);
      border: 1px solid rgba(99, 102, 241, 0.3);
      color: #a5b4fc;
      border-radius: 20px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    .status-badge {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.8rem;
      padding: 0.35rem 0.8rem;
      border-radius: 20px;
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.25);
      color: #6ee7b7;
      font-weight: 500;
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent-emerald);
      box-shadow: 0 0 8px var(--accent-emerald);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.5; transform: scale(0.85); }
    }

    /* Container & Layout */
    .container {
      max-width: 1400px;
      margin: 0 auto;
      padding: 1.75rem 2rem 4rem;
    }

    /* Navigation Tabs */
    .nav-tabs {
      display: flex;
      gap: 0.5rem;
      margin-bottom: 1.75rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 0.75rem;
    }
    .tab-btn {
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-family: inherit;
      font-size: 0.95rem;
      font-weight: 600;
      padding: 0.6rem 1.2rem;
      border-radius: 8px;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .tab-btn:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.04);
    }
    .tab-btn.active {
      color: #fff;
      background: rgba(99, 102, 241, 0.18);
      border: 1px solid rgba(99, 102, 241, 0.35);
    }

    /* Grid KPI Cards */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 1.25rem;
      margin-bottom: 2rem;
    }
    .kpi-card {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1.25rem;
      position: relative;
      overflow: hidden;
      transition: transform 0.2s, border-color 0.2s;
    }
    .kpi-card:hover {
      transform: translateY(-2px);
      border-color: rgba(255, 255, 255, 0.15);
    }
    .kpi-card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: linear-gradient(90deg, transparent, var(--accent-indigo), transparent);
      opacity: 0.6;
    }
    .kpi-label {
      font-size: 0.8rem;
      color: var(--text-muted);
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 0.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .kpi-value {
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .kpi-sub {
      font-size: 0.75rem;
      color: var(--text-dim);
      margin-top: 0.35rem;
    }
    .pos { color: var(--accent-emerald); }
    .neg { color: var(--accent-rose); }
    .neutral { color: var(--accent-cyan); }

    /* Section Cards */
    .dashboard-row {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 1.5rem;
      margin-bottom: 2rem;
    }
    @media (max-width: 1024px) {
      .dashboard-row { grid-template-columns: 1fr; }
    }

    .card {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.5rem;
      box-shadow: 0 10px 30px rgba(0,0,0,0.25);
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
    }
    .card-title {
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }

    /* Chart Canvas Container */
    .chart-container {
      width: 100%;
      height: 280px;
      position: relative;
    }
    svg.equity-svg {
      width: 100%;
      height: 100%;
      overflow: visible;
    }

    /* Backtest Control Box */
    .control-box {
      background: var(--bg-card);
      border-radius: 12px;
      padding: 1.25rem;
      border: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }
    .input-group {
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
    }
    .input-label {
      font-size: 0.8rem;
      color: var(--text-muted);
      font-weight: 500;
    }
    .input-field {
      background: rgba(10, 13, 20, 0.7);
      border: 1px solid var(--border);
      color: #fff;
      font-family: 'JetBrains Mono', monospace;
      padding: 0.6rem 0.8rem;
      border-radius: 8px;
      font-size: 0.9rem;
      outline: none;
      transition: border-color 0.2s;
    }
    .input-field:focus {
      border-color: var(--accent-indigo);
    }
    .btn-run {
      background: linear-gradient(135deg, var(--accent-indigo), #4f46e5);
      color: #fff;
      border: none;
      font-family: inherit;
      font-weight: 600;
      font-size: 0.95rem;
      padding: 0.75rem 1.25rem;
      border-radius: 8px;
      cursor: pointer;
      transition: all 0.2s;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      box-shadow: 0 4px 15px rgba(99, 102, 241, 0.35);
    }
    .btn-run:hover {
      filter: brightness(1.15);
      transform: translateY(-1px);
    }
    .btn-run:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }

    /* Trades Table */
    .table-container {
      overflow-x: auto;
      border-radius: 10px;
      border: 1px solid var(--border);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.875rem;
    }
    th {
      background: var(--bg-card);
      color: var(--text-muted);
      font-weight: 600;
      padding: 0.85rem 1rem;
      text-transform: uppercase;
      font-size: 0.72rem;
      letter-spacing: 0.05em;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 0.85rem 1rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.82rem;
    }
    tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }
    .badge {
      display: inline-block;
      padding: 0.2rem 0.55rem;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge-long { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .badge-short { background: rgba(244, 63, 94, 0.15); color: #fb7185; }
    .badge-tp { background: rgba(6, 182, 212, 0.15); color: #38bdf8; }
    .badge-sl { background: rgba(244, 63, 94, 0.15); color: #fda4af; }

    /* Code Snippets & Guide */
    .code-block {
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85rem;
      line-height: 1.6;
      color: #e2e8f0;
      overflow-x: auto;
      margin: 1rem 0;
      position: relative;
    }
    .guide-step {
      display: flex;
      gap: 1.25rem;
      margin-bottom: 1.5rem;
    }
    .step-number {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: rgba(99, 102, 241, 0.2);
      border: 1px solid rgba(99, 102, 241, 0.4);
      color: #a5b4fc;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 0.9rem;
      flex-shrink: 0;
    }
    .step-content {
      flex: 1;
    }
    .step-title {
      font-weight: 700;
      font-size: 1.05rem;
      margin-bottom: 0.4rem;
      color: #fff;
    }
    .step-desc {
      color: var(--text-muted);
      font-size: 0.9rem;
      line-height: 1.5;
    }

    /* Decision Reasons Pill List */
    .reason-list {
      display: flex;
      flex-direction: column;
      gap: 0.6rem;
    }
    .reason-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.6rem 0.8rem;
      background: var(--bg-card);
      border-radius: 8px;
      font-size: 0.82rem;
    }
    .reason-name {
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
    }
    .reason-count {
      font-weight: 700;
      color: var(--accent-cyan);
    }

    .tab-content { display: none; }
    .tab-content.active { display: block; }
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <div class="brand-icon">FX</div>
      <div>
        <div class="brand-name">FXAgent Terminal</div>
      </div>
      <span class="brand-tag">Risk-First Architecture</span>
    </div>

    <div class="header-actions">
      <div class="status-badge">
        <div class="pulse-dot"></div>
        <span>Engine Active (Zero Lookahead)</span>
      </div>
    </div>
  </header>

  <div class="container">
    <!-- Tabs Navigation -->
    <div class="nav-tabs">
      <button class="tab-btn active" onclick="switchTab('dashboard')">
        <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>
        Simulation Dashboard
      </button>
      <button class="tab-btn" onclick="switchTab('trades')">
        <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/></svg>
        Trades Ledger (<span id="tab-trade-count">0</span>)
      </button>
      <button class="tab-btn" onclick="switchTab('live-guide')">
        <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
        Live / Paper Trading Connection
      </button>
    </div>

    <!-- TAB 1: DASHBOARD -->
    <div id="tab-dashboard" class="tab-content active">
      <!-- MT5 Live Scalper Monitor Banner -->
      <div id="mt5-banner" style="background: linear-gradient(135deg, rgba(99, 102, 241, 0.15), rgba(6, 182, 212, 0.15)); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 12px; padding: 1rem 1.25rem; margin-bottom: 1.5rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
        <div style="display: flex; align-items: center; gap: 0.85rem;">
          <div id="mt5-pulse" class="pulse-dot" style="background: var(--accent-emerald); box-shadow: 0 0 10px var(--accent-emerald);"></div>
          <div>
            <div style="font-weight: 700; font-size: 0.95rem; display: flex; align-items: center; gap: 0.5rem;">
              <span>MetaTrader 5 Auto-Scalper Bridge</span>
              <span id="mt5-mode-tag" class="badge badge-long">STANDBY</span>
            </div>
            <div id="mt5-subtext" style="font-size: 0.78rem; color: var(--text-muted); margin-top: 0.2rem;">
              Run <code>python mt5_trader.py --symbol XAUUSD --timeframe M5 --mode paper</code> to start auto-trading
            </div>
          </div>
        </div>
        <div id="mt5-tickers" style="display: flex; gap: 1rem; font-family: 'JetBrains Mono', monospace; font-size: 0.82rem;">
          <!-- Live Symbol Ticker -->
        </div>
      </div>

      <!-- KPIs -->
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Total Return <span id="kpi-ret-badge" class="badge"></span></div>
          <div class="kpi-value" id="kpi-net-pnl">$0.00</div>
          <div class="kpi-sub" id="kpi-ret-pct">0.0% on $10,000</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Win Rate</div>
          <div class="kpi-value neutral" id="kpi-winrate">0.0%</div>
          <div class="kpi-sub" id="kpi-win-count">0 Wins / 0 Losses</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Max Drawdown</div>
          <div class="kpi-value neg" id="kpi-mdd">0.0%</div>
          <div class="kpi-sub">Limit: 15.0% (Kill-switch safe)</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Expectancy (R)</div>
          <div class="kpi-value" id="kpi-exp-r">0.00 R</div>
          <div class="kpi-sub">Quarter-Kelly ATR Sized</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Profit Factor</div>
          <div class="kpi-value" id="kpi-pf">0.00</div>
          <div class="kpi-sub">Gross Win / Gross Loss</div>
        </div>
      </div>

      <!-- Main Visual Row -->
      <div class="dashboard-row">
        <!-- Equity Curve Chart -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z"/></svg>
              Cumulative Portfolio Equity Curve
            </div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">
              XAUUSD + GBPUSD Combined
            </div>
          </div>
          <div class="chart-container" id="chart-box">
            <svg class="equity-svg" id="equity-svg"></svg>
          </div>
        </div>

        <!-- Backtest Trigger Form -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"/></svg>
              Run Instant Simulation
            </div>
          </div>
          <div class="control-box">
            <div class="input-group">
              <label class="input-label">Bars to Simulate (Hourly)</label>
              <input type="number" id="inp-bars" class="input-field" value="8000" step="500">
            </div>
            <div class="input-group">
              <label class="input-label">Initial Equity ($)</label>
              <input type="number" id="inp-equity" class="input-field" value="10000" step="1000">
            </div>
            <div class="input-group">
              <label class="input-label">Market Momentum / Autocorr (0 = zero edge)</label>
              <input type="number" id="inp-momentum" class="input-field" value="0.0" step="0.05" min="-0.5" max="0.5">
            </div>
            <button class="btn-run" id="btn-run" onclick="triggerRun()">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/></svg>
              Execute Backtest
            </button>
            <div id="run-status" style="font-size: 0.8rem; color: var(--accent-cyan); text-align: center; min-height: 1.2rem;"></div>
          </div>
        </div>
      </div>

      <!-- Instrument Breakdown & Filter Rejections -->
      <div class="dashboard-row">
        <!-- Instrument Cards -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">Instrument Breakdown</div>
          </div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
            <div style="background: var(--bg-card); padding: 1rem; border-radius: 10px; border: 1px solid var(--border);">
              <div style="font-weight: 700; color: #fbbf24; margin-bottom: 0.5rem; display: flex; justify-content: space-between;">
                <span>XAUUSD (Gold)</span>
                <span id="xau-ret" class="badge"></span>
              </div>
              <div style="font-size: 0.85rem; color: var(--text-muted); line-height: 1.7;">
                Trades: <strong id="xau-trades" style="color: #fff;">0</strong><br>
                Win Rate: <strong id="xau-wr" style="color: #fff;">0%</strong><br>
                Net PnL: <strong id="xau-pnl" style="color: #fff;">$0.00</strong><br>
                Max Drawdown: <strong id="xau-mdd" style="color: #fff;">0%</strong>
              </div>
            </div>
            <div style="background: var(--bg-card); padding: 1rem; border-radius: 10px; border: 1px solid var(--border);">
              <div style="font-weight: 700; color: #60a5fa; margin-bottom: 0.5rem; display: flex; justify-content: space-between;">
                <span>GBPUSD (Cable)</span>
                <span id="gbp-ret" class="badge"></span>
              </div>
              <div style="font-size: 0.85rem; color: var(--text-muted); line-height: 1.7;">
                Trades: <strong id="gbp-trades" style="color: #fff;">0</strong><br>
                Win Rate: <strong id="gbp-wr" style="color: #fff;">0%</strong><br>
                Net PnL: <strong id="gbp-pnl" style="color: #fff;">$0.00</strong><br>
                Max Drawdown: <strong id="gbp-mdd" style="color: #fff;">0%</strong>
              </div>
            </div>
          </div>
        </div>

        <!-- Filter & Veto Reasons -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">Gate Rejections (Risk Protection)</div>
          </div>
          <div class="reason-list" id="rejection-list">
            <!-- Dynamically populated -->
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 2: TRADES LEDGER -->
    <div id="tab-trades" class="tab-content">
      <div class="card">
        <div class="card-header">
          <div class="card-title">Completed Trades Log (Latest 50 Executions)</div>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Lots</th>
                <th>Entry Px</th>
                <th>Exit Px</th>
                <th>PnL ($)</th>
                <th>Return (R)</th>
                <th>Exit Reason</th>
                <th>Conf / p_win</th>
                <th>Regime</th>
              </tr>
            </thead>
            <tbody id="trades-body">
              <!-- Populated by JS -->
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 3: LIVE & PAPER TRADING CONNECTION -->
    <div id="tab-live-guide" class="tab-content">
      <div class="card" style="margin-bottom: 1.5rem;">
        <div class="card-header">
          <div class="card-title">How to Connect FXAgent to Real / Paper Brokers</div>
        </div>
        <div class="guide-step">
          <div class="step-number">1</div>
          <div class="step-content">
            <div class="step-title">The Architecture of Execution</div>
            <div class="step-desc">
              <code>fxagent</code> is broker-agnostic. The core engine ingests <strong>Bars</strong> and produces <strong>Orders</strong>.
              To trade live or in paper mode, you run a 1-hour polling loop (or WebSocket listener) that:
              <ol style="margin: 0.5rem 0 0 1.25rem; line-height: 1.6;">
                <li>Fetches the newly closed candle from your broker.</li>
                <li>Passes it to <code>agent.on_bar(bar, now, account_state, active_position)</code>.</li>
                <li>If the result is <code>action.kind == 'enter'</code>, sends the order with its stop-loss and take-profit to the broker.</li>
                <li>If <code>action.kind == 'exit'</code>, sends a close request for the active position.</li>
              </ol>
            </div>
          </div>
        </div>

        <div class="guide-step">
          <div class="step-number">2</div>
          <div class="step-content">
            <div class="step-title">MetaTrader 5 (MT5) Integration Example</div>
            <div class="step-desc">
              Using the standard <code>MetaTrader5</code> Python package, you can connect directly to any broker (IC Markets, Pepperstone, FTMO, etc.) in demo/paper mode:
            </div>
            <div class="code-block">
import MetaTrader5 as mt5
import time
from fxagent.agent import Agent
from fxagent.config import INSTRUMENTS, RiskLimits
from fxagent.decider import BaselineDecider
from fxagent.risk import RiskManager
from fxagent.models import AccountState, Position
from fxagent.state_engine import Bar

# 1. Initialize broker connection
mt5.initialize()
spec = INSTRUMENTS["XAUUSD"]
agent = Agent(spec, BaselineDecider(), RiskManager(RiskLimits()))

def sync_and_trade():
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H1, 0, 1)[0]
    bar = Bar(ts=int(rates['time']), open=rates['open'], high=rates['high'],
              low=rates['low'], close=rates['close'], volume=rates['tick_volume'])
    
    info = mt5.account_info()
    acct = AccountState(equity=info.equity)
    
    action = agent.on_bar(bar, now=int(time.time()), account=acct)
    if action.kind == "enter":
        order = action.order
        # Dispatch mt5.order_send() with order.lots, order.stop_loss, order.take_profit
        print(f"PLACING REAL ORDER: {order.direction} {order.lots} lots at {order.entry}")
            </div>
          </div>
        </div>

        <div class="guide-step">
          <div class="step-number">3</div>
          <div class="step-content">
            <div class="step-title">Interactive Brokers (IBKR) & CCXT / OANDA</div>
            <div class="step-desc">
              For Interactive Brokers (via <code>ib_insync</code>) or crypto/forex brokers via <code>ccxt</code>, the exact same pattern applies: translate the broker's bar event to <code>fxagent.state_engine.Bar</code>, pass account equity to <code>AccountState</code>, and let the agent manage the position lifecycle.
            </div>
          </div>
        </div>
      </div>
    </div>

  </div>

  <script>
    let appData = null;

    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      event.currentTarget.classList.add('active');
      document.getElementById('tab-' + tabId).classList.add('active');
    }

    async function loadData() {
      try {
        const res = await fetch('/api/data');
        appData = await res.json();
        renderDashboard();
      } catch (e) {
        console.error("Failed to load dashboard data", e);
      }
    }

    function renderDashboard() {
      if (!appData) return;
      const o = appData.overall;
      const xau = appData.xau;
      const gbp = appData.gbp;

      // MT5 Live Status
      const mt5 = appData.mt5_status;
      const tag = document.getElementById('mt5-mode-tag');
      const sub = document.getElementById('mt5-subtext');
      const tickers = document.getElementById('mt5-tickers');
      if (mt5 && (Date.now() / 1000 - mt5.timestamp < 30)) {
        tag.textContent = (mt5.mode || 'LIVE').toUpperCase() + ' ' + (mt5.timeframe || 'M5') + (mt5.mock ? ' [SIMULATED]' : ' [MT5 LIVE]');
        tag.className = 'badge ' + (mt5.mode === 'live' ? 'badge-short' : 'badge-long');
        sub.innerHTML = `Account Equity: <strong>$${mt5.account.equity.toFixed(2)}</strong> | Peak: <strong>$${mt5.account.peak_equity.toFixed(2)}</strong> | Scalping Timeframe: <strong>${mt5.timeframe}</strong>`;
        tickers.innerHTML = '';
        for (const [sym, info] of Object.entries(mt5.symbols)) {
          const div = document.createElement('div');
          div.style.background = 'rgba(0,0,0,0.3)';
          div.style.padding = '0.35rem 0.75rem';
          div.style.borderRadius = '6px';
          div.style.border = '1px solid var(--border)';
          const dirColor = info.position_dir === 'LONG' ? 'var(--accent-emerald)' : (info.position_dir === 'SHORT' ? 'var(--accent-rose)' : 'var(--text-muted)');
          div.innerHTML = `<span style="color:#fbbf24; font-weight:700;">${sym}:</span> ${info.bid.toFixed(sym === 'XAUUSD'?2:5)} | <span style="color:${dirColor}; font-weight:600;">${info.position_dir}</span> ${info.position_lots ? '(' + info.position_lots + 'L)' : ''}`;
          tickers.appendChild(div);
        }
      } else {
        tag.textContent = 'STANDBY';
        tag.className = 'badge';
        sub.innerHTML = 'Run <code>python mt5_trader.py --symbol XAUUSD --timeframe M5 --mode paper</code> in PowerShell to activate';
        tickers.innerHTML = '';
      }

      // KPIs
      const pnlEl = document.getElementById('kpi-net-pnl');
      pnlEl.textContent = (o.net_pnl >= 0 ? '+$' : '-$') + Math.abs(o.net_pnl).toFixed(2);
      pnlEl.className = 'kpi-value ' + (o.net_pnl >= 0 ? 'pos' : 'neg');

      const retPctEl = document.getElementById('kpi-ret-pct');
      retPctEl.textContent = `${o.return_pct > 0 ? '+' : ''}${o.return_pct}% on $10,000`;

      document.getElementById('kpi-winrate').textContent = `${o.win_rate}%`;
      document.getElementById('kpi-win-count').textContent = `${o.wins} Wins / ${o.losses} Losses`;
      document.getElementById('kpi-mdd').textContent = `${o.max_drawdown_pct}%`;
      document.getElementById('kpi-exp-r').textContent = `${o.expectancy_r > 0 ? '+' : ''}${o.expectancy_r} R`;
      document.getElementById('kpi-pf').textContent = `${o.profit_factor}`;

      // Instruments
      document.getElementById('xau-trades').textContent = xau.trades;
      document.getElementById('xau-wr').textContent = xau.win_rate + '%';
      document.getElementById('xau-pnl').textContent = (xau.net_pnl >= 0 ? '+$' : '-$') + Math.abs(xau.net_pnl).toFixed(2);
      document.getElementById('xau-mdd').textContent = xau.max_drawdown_pct + '%';
      const xauRet = document.getElementById('xau-ret');
      xauRet.textContent = (xau.return_pct >= 0 ? '+' : '') + xau.return_pct + '%';
      xauRet.className = 'badge ' + (xau.return_pct >= 0 ? 'badge-long' : 'badge-short');

      document.getElementById('gbp-trades').textContent = gbp.trades;
      document.getElementById('gbp-wr').textContent = gbp.win_rate + '%';
      document.getElementById('gbp-pnl').textContent = (gbp.net_pnl >= 0 ? '+$' : '-$') + Math.abs(gbp.net_pnl).toFixed(2);
      document.getElementById('gbp-mdd').textContent = gbp.max_drawdown_pct + '%';
      const gbpRet = document.getElementById('gbp-ret');
      gbpRet.textContent = (gbp.return_pct >= 0 ? '+' : '') + gbp.return_pct + '%';
      gbpRet.className = 'badge ' + (gbp.return_pct >= 0 ? 'badge-long' : 'badge-short');

      // Rejections
      const rList = document.getElementById('rejection-list');
      rList.innerHTML = '';
      const allReasons = Object.assign({}, appData.decisions_xau.reasons, appData.decisions_gbp.reasons);
      const sortedReasons = Object.entries(allReasons).sort((a,b) => b[1] - a[1]);
      for (const [r, count] of sortedReasons.slice(0, 5)) {
        const div = document.createElement('div');
        div.className = 'reason-item';
        div.innerHTML = `<span class="reason-name">${r}</span><span class="reason-count">${count} bars</span>`;
        rList.appendChild(div);
      }

      // Trades Ledger
      document.getElementById('tab-trade-count').textContent = appData.total_trades_count;
      const tBody = document.getElementById('trades-body');
      tBody.innerHTML = '';
      const revTrades = [...appData.trades].reverse();
      for (const t of revTrades) {
        const tr = document.createElement('tr');
        const isWin = t.pnl > 0;
        const sideBadge = t.direction === 1 ? '<span class="badge badge-long">LONG</span>' : '<span class="badge badge-short">SHORT</span>';
        const exitBadge = t.reason === 'target' ? '<span class="badge badge-tp">TARGET</span>' : '<span class="badge badge-sl">STOP</span>';
        tr.innerHTML = `
          <td><strong>${t.symbol}</strong></td>
          <td>${sideBadge}</td>
          <td>${t.lots}</td>
          <td>${t.entry.toFixed(t.symbol === 'XAUUSD' ? 2 : 5)}</td>
          <td>${t.exit.toFixed(t.symbol === 'XAUUSD' ? 2 : 5)}</td>
          <td class="${isWin ? 'pos' : 'neg'}">${(t.pnl >= 0 ? '+$' : '-$') + Math.abs(t.pnl).toFixed(2)}</td>
          <td class="${t.r >= 0 ? 'pos' : 'neg'}">${(t.r >= 0 ? '+' : '') + t.r.toFixed(2)} R</td>
          <td>${exitBadge}</td>
          <td>${(t.confidence * 100).toFixed(0)}% / ${(t.p_win * 100).toFixed(0)}%</td>
          <td><span style="color: var(--accent-purple);">${t.regime}</span></td>
        `;
        tBody.appendChild(tr);
      }

      // Equity Chart
      drawEquityChart(o.curve);
    }

    function drawEquityChart(curve) {
      const svg = document.getElementById('equity-svg');
      if (!curve || curve.length < 2) {
        svg.innerHTML = '<text x="50%" y="50%" fill="#64748b" text-anchor="middle">No trade history</text>';
        return;
      }
      const rect = svg.getBoundingClientRect();
      const w = rect.width || 700;
      const h = rect.height || 260;
      const pad = 40;

      const eqs = curve.map(c => c.equity);
      const minEq = Math.min(...eqs) * 0.995;
      const maxEq = Math.max(...eqs) * 1.005;

      const pts = curve.map((c, i) => {
        const x = pad + (i / (curve.length - 1)) * (w - 2 * pad);
        const y = h - pad - ((c.equity - minEq) / (maxEq - minEq)) * (h - 2 * pad);
        return [x, y];
      });

      const pathStr = pts.reduce((acc, p, i) => i === 0 ? `M ${p[0]} ${p[1]}` : `${acc} L ${p[0]} ${p[1]}`, '');
      const areaStr = `${pathStr} L ${pts[pts.length-1][0]} ${h - pad} L ${pts[0][0]} ${h - pad} Z`;

      const isPositive = curve[curve.length - 1].equity >= curve[0].equity;
      const strokeColor = isPositive ? '#10b981' : '#f43f5e';
      const gradColor = isPositive ? 'rgba(16, 185, 129, 0.2)' : 'rgba(244, 63, 94, 0.2)';

      svg.innerHTML = `
        <defs>
          <linearGradient id="curveGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${gradColor}"/>
            <stop offset="100%" stop-color="transparent"/>
          </linearGradient>
        </defs>
        <!-- Grid lines -->
        <line x1="${pad}" y1="${pad}" x2="${w - pad}" y2="${pad}" stroke="rgba(255,255,255,0.05)" />
        <line x1="${pad}" y1="${h/2}" x2="${w - pad}" y2="${h/2}" stroke="rgba(255,255,255,0.05)" />
        <line x1="${pad}" y1="${h - pad}" x2="${w - pad}" y2="${h - pad}" stroke="rgba(255,255,255,0.08)" />

        <text x="${w - pad}" y="${pad + 12}" fill="#64748b" font-size="11" text-anchor="end">$${maxEq.toFixed(0)}</text>
        <text x="${w - pad}" y="${h - pad - 6}" fill="#64748b" font-size="11" text-anchor="end">$${minEq.toFixed(0)}</text>

        <path d="${areaStr}" fill="url(#curveGrad)" />
        <path d="${pathStr}" fill="none" stroke="${strokeColor}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
      `;
    }

    async function triggerRun() {
      const btn = document.getElementById('btn-run');
      const status = document.getElementById('run-status');
      const bars = document.getElementById('inp-bars').value;
      const eq = document.getElementById('inp-equity').value;
      const mom = document.getElementById('inp-momentum').value;

      btn.disabled = true;
      btn.innerHTML = 'Simulating...';
      status.textContent = 'Running simulation through state engine & risk manager...';

      try {
        const res = await fetch('/api/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bars: parseInt(bars), equity: parseFloat(eq), momentum: parseFloat(mom) })
        });
        const d = await res.json();
        status.textContent = 'Simulation completed successfully!';
        await loadData();
      } catch (err) {
        status.textContent = 'Error executing backtest: ' + err;
      } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/></svg> Execute Backtest`;
        setTimeout(() => { status.textContent = ''; }, 4000);
      }
    }

    window.addEventListener('load', () => {
      loadData();
      setInterval(loadData, 2500);
    });
    window.addEventListener('resize', () => { if (appData) drawEquityChart(appData.overall.curve); });
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            body = HTML_CONTENT.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/data":
            self._send_json(get_dashboard_data())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/run":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            bars = payload.get("bars", 8000)
            equity = payload.get("equity", 10000.0)
            momentum = payload.get("momentum", 0.0)

            cmd = [
                sys.executable,
                os.path.join(BASE_DIR, "run_backtest.py"),
                "--bars", str(bars),
                "--equity", str(equity),
                "--momentum", str(momentum),
            ]
            p = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
            if p.returncode == 0:
                self._send_json({"status": "ok", "output": p.stdout})
            else:
                self._send_json({"status": "error", "error": p.stderr}, code=500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy standard request logging
        pass


def run_server():
    server_address = ("", PORT)
    httpd = HTTPServer(server_address, DashboardHandler)
    print(f"\n========================================================")
    print(f" FXAGENT LOCALHOST DASHBOARD READY")
    print(f" URL: http://localhost:{PORT}")
    print(f" Press Ctrl+C in your PowerShell window to stop")
    print(f"========================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
