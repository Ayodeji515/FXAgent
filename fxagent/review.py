"""Nightly review: measure calibration with the Brier score and PROPOSE new parameters.

A proposal is written as pending. Nothing goes live until the operator approves it
(`--approve`). The loop compounds only through reviewed, versioned parameter files, never by
letting the system rewrite its own risk limits.

Caveat: calibrating on executed trades only has selection bias (you only see the setups that
passed the gate). Add counterfactual labels for gated-out candidates before trusting the fit.
"""

import argparse
import glob
import json
import os


def brier_score(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def reliability(ps, ys, bins=5):
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        idx = [k for k, p in enumerate(ps) if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if idx:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                        "mean_p": round(sum(ps[k] for k in idx) / len(idx), 3),
                        "win_rate": round(sum(ys[k] for k in idx) / len(idx), 3)})
    return out


def _apply(p, alpha, bias):
    return max(0.0, min(1.0, 0.5 + alpha * (p - 0.5) + bias))


def fit_calibration(ps, ys, min_n=30):
    """Grid-search a shrink (alpha) and bias minimizing Brier. Refuses to fit on thin data."""
    if len(ps) < min_n:
        return None
    best = (brier_score(ps, ys), 1.0, 0.0)
    for a in [i * 0.05 for i in range(0, 31)]:
        for b in [i * 0.01 for i in range(-10, 11)]:
            s = brier_score([_apply(p, a, b) for p in ps], ys)
            if s < best[0] - 1e-12:
                best = (s, a, b)
    return {"alpha": round(best[1], 2), "bias": round(best[2], 2), "brier_after": round(best[0], 5)}


def review_trades(trades):
    ps = [t["p_win"] for t in trades]
    ys = [1 if t["pnl"] > 0 else 0 for t in trades]
    base = sum(ys) / len(ys)
    b = brier_score(ps, ys)
    b_ref = brier_score([base] * len(ys), ys)
    return {
        "n": len(ys),
        "win_rate": round(base, 3),
        "brier": round(b, 5),
        "brier_base_rate": round(b_ref, 5),
        "skill_vs_base_rate": round(1 - b / b_ref, 4) if b_ref > 0 else None,
        "avg_r": round(sum(t["r"] for t in trades) / len(trades), 3),
        "reliability": reliability(ps, ys),
    }


def _versions(params_dir):
    files = glob.glob(os.path.join(params_dir, "params_v*.json"))
    return sorted(int(os.path.basename(f)[8:-5]) for f in files)


def propose(trades_path, params_dir, min_n=30):
    with open(trades_path) as f:
        trades = [json.loads(line) for line in f if line.strip()]
    if not trades:
        return {"status": "no_trades"}
    report = review_trades(trades)
    fit = fit_calibration([t["p_win"] for t in trades],
                          [1 if t["pnl"] > 0 else 0 for t in trades], min_n=min_n)
    if fit is None:
        return {"status": "insufficient_data", "report": report, "min_n": min_n}
    os.makedirs(params_dir, exist_ok=True)
    vs = _versions(params_dir)
    nxt = (vs[-1] + 1) if vs else 1
    proposal = {"version": nxt, "status": "pending_approval",
                "calibration": {"alpha": fit["alpha"], "bias": fit["bias"]}, "report": report,
                "brier_after": fit["brier_after"]}
    with open(os.path.join(params_dir, f"params_v{nxt}.json"), "w") as f:
        json.dump(proposal, f, indent=2)
    return {"status": "proposed", "version": nxt, "report": report, "fit": fit}


def approve_latest(params_dir):
    vs = _versions(params_dir)
    if not vs:
        return None
    path = os.path.join(params_dir, f"params_v{vs[-1]}.json")
    with open(path) as f:
        p = json.load(f)
    p["status"] = "active"
    with open(path, "w") as f:
        json.dump(p, f, indent=2)
    return p


def load_active_calibration(params_dir):
    """Highest-numbered approved version, or None (use defaults)."""
    for v in reversed(_versions(params_dir)):
        with open(os.path.join(params_dir, f"params_v{v}.json")) as f:
            p = json.load(f)
        if p.get("status") == "active":
            return p["calibration"]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trades", required=True, help="trades.jsonl written by run_backtest.py or live logger")
    ap.add_argument("--params-dir", default="params")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--approve", action="store_true", help="activate the latest pending proposal")
    args = ap.parse_args()
    if args.approve:
        print(json.dumps(approve_latest(args.params_dir), indent=2))
        return
    print(json.dumps(propose(args.trades, args.params_dir, args.min_n), indent=2))


if __name__ == "__main__":
    main()
