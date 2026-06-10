#!/usr/bin/env python3
"""
BTC-Alt Strategy Pipeline — No Hyperopt
Iterative: backtest → analyze → tweak → backtest
Promotes strategies that meet BTC-profit criteria to dry-run containers.

Usage:
  .venv/bin/python scripts/btc_alt_pipeline.py --strategy MyBtcAltStrategy --timerange 20250101-20260531
  .venv/bin/python scripts/btc_alt_pipeline.py --strategy MyBtcAltStrategy --config user_data/config_btc_spot_my.json --promote

Criteria (BTC-denominated):
  - min_profit_pct: 2.0% (BTC profit over backtest period)
  - min_trades: 100
  - max_dd_pct: 15.0%
  - min_win_rate: 40%
  - min_profit_factor: 1.1
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
VENV_BIN = ROOT / ".venv" / "bin"
FR_BIN = VENV_BIN / "freqtrade"
PIPELINE_STATE = USER_DATA / "btc_alt_pipeline_state.json"


# ── Datatypes ─────────────────────────────────────────────────────────────────
@dataclass
class BacktestResult:
    trades: int = 0
    profit_abs_btc: float = 0.0
    profit_pct: float = 0.0
    max_dd_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_profit_pct: float = 0.0
    avg_duration: str = ""
    sharpe: float = 0.0
    best_pair: str = ""
    worst_pair: str = ""
    raw: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────
def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_json(p: Path, d: dict):
    p.write_text(json.dumps(d, indent=2, default=str) + "\n", encoding="utf-8")


def parse_backtest(output: str) -> BacktestResult:
    r = BacktestResult(raw=output)

    def g(pattern, cast=float):
        m = re.search(pattern, output, re.MULTILINE)
        return cast(m.group(1).replace(",", "")) if m else 0.0

    r.trades = g(r"Total/Daily Avg Trades\s*│\s*([0-9]+)\s*/", int)
    r.profit_abs_btc = g(r"Absolute profit\s*│\s*([-0-9.]+)\s+BTC", float)
    r.profit_pct = g(r"Total profit %\s*│\s*([-0-9.]+)%")
    r.max_dd_pct = g(r"Max % of account underwater\s*│\s*([-0-9.]+)%")
    r.sharpe = g(r"Sharpe\s*│\s*([0-9.-]+)")
    r.profit_factor = g(r"Profit factor\s*│\s*([0-9.]+)")

    # win rate from the summary table
    m = re.search(r"(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)", output.split("STRATEGY SUMMARY")[-1] if "STRATEGY SUMMARY" in output else "")
    if m:
        wins, draws, losses = int(m.group(1)), int(m.group(2)), int(m.group(3))
        total = wins + draws + losses
        r.win_rate = (wins / total * 100) if total > 0 else 0

    # avg profit
    r.avg_profit_pct = g(r"Avg Profit %\s*│\s*([-0-9.]+)%") or g(r"Avg profit %\s*│\s*([-0-9.]+)")
    if not r.avg_profit_pct:
        m = re.search(r"Avg Profit %\s*\x1b\[[0-9;]*m([-0-9.]+)", output)
        if m: r.avg_profit_pct = float(m.group(1))

    return r


# ── Backtest ──────────────────────────────────────────────────────────────────
def run_backtest(config_path: str, strategy: str, timerange: str,
                 pairs: str | None = None) -> BacktestResult:
    cmd = [str(FR_BIN), "backtesting",
           "--config", config_path,
           "--strategy", strategy,
           "--timerange", timerange,
           "--cache", "none",
           "--export", "none"]
    if pairs:
        cmd += ["--pairs"] + pairs.split()

    proc = run(cmd)
    merged = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        print(f"  Backtest failed (exit {proc.returncode})")
        print(f"  {proc.stderr[-300:]}")
        return BacktestResult(raw=merged)
    return parse_backtest(merged)


# ── Judge ────────────────────────────────────────────────────────────────────
def judge(r: BacktestResult, criteria: dict) -> tuple[bool, list[str]]:
    reasons = []
    passed = True

    checks = [
        ("trades", r.trades, ">=", criteria.get("min_trades", 100)),
        ("profit_pct", r.profit_pct, ">=", criteria.get("min_profit_pct", 2.0)),
        ("win_rate", r.win_rate, ">=", criteria.get("min_win_rate", 40)),
        ("max_dd_pct", r.max_dd_pct, "<=", criteria.get("max_dd_pct", 15.0)),
        ("profit_factor", r.profit_factor, ">=", criteria.get("min_profit_factor", 1.1)),
    ]

    for name, actual, op, target in checks:
        ok = (op == ">=" and actual >= target) or (op == "<=" and actual <= target)
        if not ok:
            passed = False
            reasons.append(f"{name}={actual:.2f} {op} {target}")

    return passed, reasons


# ── Display ───────────────────────────────────────────────────────────────────
def display(r: BacktestResult, criteria: dict, run_num: int = 1):
    passed, reasons = judge(r, criteria)
    icon = "✅ PASS" if passed else "❌ FAIL"
    print(f"\n{'='*60}")
    print(f"  Run #{run_num} — {icon}")
    print(f"{'='*60}")
    print(f"  Trades:       {r.trades}")
    print(f"  Profit:       {r.profit_pct:+.2f}% ({r.profit_abs_btc:+.6f} BTC)")
    print(f"  Win Rate:     {r.win_rate:.1f}%")
    print(f"  Max DD:       {r.max_dd_pct:.2f}%")
    print(f"  Profit Factor:{r.profit_factor:.2f}")
    print(f"  Avg Profit:   {r.avg_profit_pct:+.2f}%")
    print(f"  Sharpe:       {r.sharpe:.2f}")
    if not passed:
        print(f"  Reasons:      {'; '.join(reasons)}")
    print(f"{'='*60}")
    return passed, reasons


# ── Save State ────────────────────────────────────────────────────────────────
def update_state(strategy: str, config: str, timerange: str, r: BacktestResult,
                 run_num: int, passed: bool, reasons: list[str], criteria: dict):
    state = load_json(PIPELINE_STATE)
    key = strategy
    history = state.get(key, {}).get("history", [])
    history.append({
        "run": run_num,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trades": r.trades,
        "profit_pct": r.profit_pct,
        "profit_abs_btc": r.profit_abs_btc,
        "win_rate": r.win_rate,
        "max_dd_pct": r.max_dd_pct,
        "profit_factor": r.profit_factor,
        "passed": passed,
        "reasons": reasons,
    })
    state[key] = {
        "config": config,
        "timerange": timerange,
        "last_run": run_num,
        "passed": passed,
        "history": history,
        "criteria": criteria,
    }
    save_json(PIPELINE_STATE, state)


# ── Promote ───────────────────────────────────────────────────────────────────
def promote(strategy: str, config_path: str) -> None:
    """Create a docker-compose file for dry-run and start the container."""
    strategy_lower = strategy.lower().replace("_", "-")
    compose_path = ROOT / f"docker-compose.btc-alt-{strategy_lower}.yml"

    # Read base config
    config = load_json(Path(config_path))

    compose = f"""version: '3'
services:
  freqtrade-btc-alt-{strategy_lower}:
    image: freqtrade-local:postgres
    restart: unless-stopped
    container_name: freqtrade-btc-alt-{strategy_lower}
    volumes:
      - ./user_data:/freqtrade/user_data
    ports:
      - "{next_free_port()}:8119"
    command: >
      trade
      --logfile /freqtrade/user_data/logs/btc-alt-{strategy_lower}.log
      --db-url postgresql://bits:4642@host.docker.internal:5432/freqtrade_btc_alt_{strategy_lower}
      --strategy {strategy}
      --config /freqtrade/user_data/{Path(config_path).name}
"""

    compose_path.write_text(compose)
    print(f"  Compose file: {compose_path}")

    # Start
    subprocess.run(["docker", "compose", "-f", str(compose_path), "up", "-d"],
                   cwd=str(ROOT), check=False)
    print(f"  Container started: freqtrade-btc-alt-{strategy_lower}")


def next_free_port() -> int:
    state = load_json(PIPELINE_STATE)
    used = set()
    for v in state.values():
        p = v.get("port", 0)
        if p: used.add(p)
    for p in range(8200, 8300):
        if p not in used:
            return p
    return 8200


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="BTC-Alt Strategy Pipeline — No Hyperopt")
    p.add_argument("--strategy", required=True)
    p.add_argument("--config", default="user_data/config_btc_spot_base.json")
    p.add_argument("--timerange", default="20250101-20260531")
    p.add_argument("--pairs", default=None, help="Override pair list, e.g. 'SOL/BTC ETH/BTC'")
    p.add_argument("--min-profit-pct", type=float, default=2.0)
    p.add_argument("--min-trades", type=int, default=100)
    p.add_argument("--max-dd-pct", type=float, default=15.0)
    p.add_argument("--min-win-rate", type=float, default=40)
    p.add_argument("--min-profit-factor", type=float, default=1.1)
    p.add_argument("--promote", action="store_true", help="Promote to dry-run container if passing")
    p.add_argument("--runs", type=int, default=1, help="Number of backtest runs (for iterative tweaking)")
    args = p.parse_args()

    criteria = {
        "min_profit_pct": args.min_profit_pct,
        "min_trades": args.min_trades,
        "max_dd_pct": args.max_dd_pct,
        "min_win_rate": args.min_win_rate,
        "min_profit_factor": args.min_profit_factor,
    }

    config_path = args.config if args.config.startswith("user_data/") else f"user_data/{args.config}"

    print(f"╔{'═'*58}╗")
    print(f"║  BTC-Alt Pipeline — {args.strategy}")
    print(f"║  Config: {config_path} | Timerange: {args.timerange}")
    print(f"║  Criteria: profit≥{args.min_profit_pct}% trades≥{args.min_trades} dd≤{args.max_dd_pct}% wr≥{args.min_win_rate}% pf≥{args.min_profit_factor}")
    print(f"╠{'═'*58}╣")

    for run_num in range(1, args.runs + 1):
        print(f"║  [{run_num}/{args.runs}] Backtesting...")
        r = run_backtest(config_path, args.strategy, args.timerange, args.pairs)
        if r.trades == 0 and "error" in r.raw.lower():
            print(f"║  BACKTEST FAILED — check strategy {args.strategy}")
            return 1

        passed, reasons = display(r, criteria, run_num)
        update_state(args.strategy, config_path, args.timerange, r, run_num, passed, reasons, criteria)

        if passed:
            print(f"\n  ✅ Strategy {args.strategy} PASSES criteria!")
            if args.promote:
                print(f"  🚀 Promoting to dry-run container...")
                promote(args.strategy, config_path)
            return 0

    print(f"\n  ❌ Strategy {args.strategy} did not pass after {args.runs} run(s).")
    print(f"  Tweak strategy params and re-run with --runs 1 to check.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
