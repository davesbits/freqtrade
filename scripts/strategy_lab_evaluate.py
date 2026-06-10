#!/usr/bin/env python3
"""
Strategy Research Lab — Evaluator
Runs: backtest baseline → hyperopt → backtest candidate → judge → promote
Usage:
  .venv/bin/python scripts/strategy_lab_evaluate.py \
    --strategy MyNewStrategy \
    --config user_data/config_my_new_strategy.json \
    --timerange 20250101-20260520 \
    --regime bearish
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
HYPEROPT_RESULTS = USER_DATA / "hyperopt_results"
REGISTRY_PATH = USER_DATA / "bots_registry.json"
LAB_CONFIG_PATH = USER_DATA / "strategy_lab_config.json"
VENV_BIN = ROOT / ".venv" / "bin"
FR_BIN = VENV_BIN / "freqtrade"
PY_BIN = VENV_BIN / "python"

os.environ.setdefault("PYTHONPATH", str(ROOT))


# ── Datatypes ─────────────────────────────────────────────────────────────────
@dataclass
class BacktestMetrics:
    trades: int
    profit_abs: float
    profit_pct: float
    max_underwater_pct: float
    sharpe: float = 0.0
    win_rate: float = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────
def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, check=check)


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p: Path, d: dict):
    p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def parse_backtest(output: str) -> BacktestMetrics:
    if "No trades made." in output:
        return BacktestMetrics(0, 0, 0, 0, 0, 0)

    def g(pattern, cast=float):
        m = re.search(pattern, output, re.MULTILINE)
        return cast(m.group(1).replace(",", "")) if m else 0.0

    return BacktestMetrics(
        trades=g(r"Total/Daily Avg Trades\s*│\s*([0-9]+)\s*/", int),
        profit_abs=g(r"Absolute profit\s*│\s*([-0-9.]+)\s+USDT"),
        profit_pct=g(r"Total profit %\s*│\s*([-0-9.]+)%"),
        max_underwater_pct=g(r"Max % of account underwater\s*│\s*([-0-9.]+)%"),
        sharpe=g(r"Sharpe\s*│\s*([0-9.]+)"),
        win_rate=g(r"Wins.*?│\s*([0-9]+)\s*/", int) / max(g(r"Total/Daily Avg Trades\s*│\s*([0-9]+)\s*/", int), 1) * 100,
    )


# ── Backtest ──────────────────────────────────────────────────────────────────
def run_backtest(config_path: Path, strategy_name: str, timerange: str,
                 strategy_path: Path | None = None) -> BacktestMetrics:
    cmd = [str(FR_BIN), "backtesting",
           "--config", str(config_path.relative_to(ROOT)),
           "--strategy", strategy_name,
           "--timerange", timerange,
           "--cache", "none",
           "--export", "none",
           "--timeframe-detail", "1m"]
    if strategy_path:
        cmd += ["--strategy-path", str(strategy_path.relative_to(ROOT))]
    proc = run(cmd)
    merged = proc.stdout + "\n" + proc.stderr
    return parse_backtest(merged)


# ── Hyperopt ──────────────────────────────────────────────────────────────────
def run_hyperopt(config_path: Path, strategy_name: str, timerange: str,
                 epochs: int = 500, min_trades: int = 200,
                 job_workers: int = 1,
                 spaces: str = "roi stoploss trailing") -> bool:
    cmd = [str(FR_BIN), "hyperopt",
           "--config", str(config_path.relative_to(ROOT)),
           "--strategy", strategy_name,
           "--timerange", timerange,
           "--spaces"] + spaces.split() + [
           "--epochs", str(epochs),
           "--min-trades", str(min_trades),
           "--hyperopt-loss", "ProfitDrawDownHyperOptLoss",
           "--random-state", "42",
           "--job-workers", str(job_workers),
           "--disable-param-export"]
    proc = run(cmd, check=False)
    return proc.returncode == 0


# ── Next port ─────────────────────────────────────────────────────────────────
def next_free_port(registry: dict, start: int = 8117) -> int:
    used = {int(b.get("port")) for b in registry.get("bots", [])
            if b.get("port") and str(b.get("port")).isdigit()}
    p = start
    while p in used:
        p += 1
    return p


# ── Promotion ─────────────────────────────────────────────────────────────────
def promote(config_path: Path, strategy_name: str, variant_name: str,
            port: int, baseline: BacktestMetrics, candidate: BacktestMetrics,
            regime: str) -> None:
    # Build compose file
    tag = variant_name.lower()
    compose_path = ROOT / f"docker-compose.{tag}.yml"
    compose_text = f"""services:
  {tag}:
    build:
      context: .
      dockerfile: Dockerfile
    image: {tag}:local
    container_name: {tag}
    restart: unless-stopped
    ports:
      - "{port}:{port}"
    volumes:
      - ./user_data:/freqtrade/user_data
    command: >
      trade
      --config /freqtrade/user_data/{config_path.name}
      --strategy {strategy_name}
      --db-url sqlite:////freqtrade/user_data/trades-{tag}.sqlite
"""
    compose_path.write_text(compose_text, encoding="utf-8")

    # Launch container
    run(["docker", "compose", "-f", str(compose_path), "up", "-d", "--build"], check=True)

    # Update registry
    reg = load_json(REGISTRY_PATH)
    bots = [b for b in reg.get("bots", []) if b.get("name") != tag]
    bots.append({
        "name": tag,
        "port": port,
        "strategy": strategy_name,
        "config": str(config_path.relative_to(ROOT)),
        "mode": "isolated futures dry-run",
        "status": "running",
        "regime": regime,
        "benchmark": (f"lab promoted [{regime}]: {baseline.profit_pct:.2f}% → "
                      f"{candidate.profit_pct:.2f}% | dd={candidate.max_underwater_pct:.1f}% "
                      f"| trades={candidate.trades} | sharpe={candidate.sharpe:.2f}"),
    })
    reg["bots"] = bots
    save_json(REGISTRY_PATH, reg)
    print(f"PROMOTED {strategy_name} → {tag} on port {port}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Strategy Lab Evaluate: backtest → hyperopt → promote")
    p.add_argument("--strategy", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--timerange", default="20250101-20260520")
    p.add_argument("--regime", default="neutral", choices=["bullish", "bearish", "neutral", "choppy_ranging"])
    p.add_argument("--epochs", type=int, default=500)
    p.add_argument("--min-trades", type=int, default=200)
    p.add_argument("--min-profit-pct", type=float, default=1.0)
    p.add_argument("--min-delta-pct", type=float, default=0.25)
    p.add_argument("--max-dd-pct", type=float, default=12.0)
    p.add_argument("--skip-hyperopt", action="store_true")
    p.add_argument("--skip-promote", action="store_true")
    p.add_argument("--job-workers", type=int, default=1)
    args = p.parse_args()

    config_path = USER_DATA / args.config if not args.config.startswith("/") else Path(args.config)
    strategy_name = args.strategy
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    print(f"╔══════════════════════════════════════════════════╗")
    print(f"║  Strategy Lab — {args.regime.upper()} Evaluation")
    print(f"║  Strategy: {strategy_name}")
    print(f"║  Timerange: {args.timerange}")
    print(f"╠══════════════════════════════════════════════════╣")

    # 1. Baseline Backtest
    print(f"║  [1/4] Backtesting baseline...")
    baseline = run_backtest(config_path, strategy_name, args.timerange)
    print(f"║  Baseline: profit={baseline.profit_pct:.2f}% trades={baseline.trades} dd={baseline.max_underwater_pct:.1f}%")

    # 2. Hyperopt (optional)
    if not args.skip_hyperopt:
        print(f"║  [2/4] Running hyperopt ({args.epochs} epochs)...")
        ok = run_hyperopt(config_path, strategy_name, args.timerange,
                          epochs=args.epochs, min_trades=args.min_trades,
                          job_workers=args.job_workers)
        if not ok:
            print(f"║  HYPEROPT FAILED — stopping pipeline")
            return 1
    else:
        print(f"║  [2/4] Skipping hyperopt (--skip-hyperopt)")

    # 3. Candidate Backtest
    print(f"║  [3/4] Backtesting candidate (post-hyperopt)...")
    candidate = run_backtest(config_path, strategy_name, args.timerange)
    delta = candidate.profit_pct - baseline.profit_pct
    print(f"║  Candidate: profit={candidate.profit_pct:.2f}% trades={candidate.trades} dd={candidate.max_underwater_pct:.1f}%")
    print(f"║  Delta: {delta:+.2f}%")

    # 4. Judge
    print(f"╠══════════════════════════════════════════════════╣")
    print(f"║  [4/4] Judging...")
    passed = True
    reasons = []

    if candidate.trades < args.min_trades:
        passed = False
        reasons.append(f"trades={candidate.trades} < min={args.min_trades}")
    if candidate.profit_pct < args.min_profit_pct:
        passed = False
        reasons.append(f"profit={candidate.profit_pct:.2f}% < min={args.min_profit_pct}%")
    if candidate.max_underwater_pct > args.max_dd_pct:
        passed = False
        reasons.append(f"dd={candidate.max_underwater_pct:.1f}% > max={args.max_dd_pct}%")

    if passed:
        print(f"║  ✅ PASSED — eligible for promotion")
        if not args.skip_promote:
            reg = load_json(REGISTRY_PATH)
            port = next_free_port(reg)
            variant_name = f"openclaw-lab-{strategy_name.lower()}-{ts}"
            promote(config_path, strategy_name, variant_name, port, baseline, candidate, args.regime)
            print(f"║  Launched as {variant_name}:{port}")
    else:
        print(f"║  ❌ REJECTED: {'; '.join(reasons)}")

    # Write report
    report = {
        "timestamp": ts,
        "strategy": strategy_name,
        "config": str(config_path),
        "regime": args.regime,
        "timerange": args.timerange,
        "baseline": asdict(baseline),
        "candidate": asdict(candidate),
        "delta_pct": delta,
        "passed": passed,
        "reasons": reasons,
    }
    report_path = USER_DATA / "hyperopt_runs" / f"lab_eval_{strategy_name}_{ts}.json"
    save_json(report_path, report)
    print(f"╚══════════════════════════════════════════════════╝")
    print(f"Report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())