#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
HYPEROPT_RESULTS = USER_DATA / "hyperopt_results"
REGISTRY_PATH = USER_DATA / "bots_registry.json"

BASE_STRATEGY = "LiquiditySweepScalper"
BASE_STRATEGY_FILE = STRATEGY_DIR / f"{BASE_STRATEGY}.py"
BASE_CONFIG_PATH = USER_DATA / "config_openclaw-liquidity-scalper.json"
BASE_COMPOSE_PATH = ROOT / "docker-compose.openclaw-liquidity-scalper.yml"
DEFAULT_RUNTIME_IMAGE = os.getenv("LIQUIDITY_RUNTIME_IMAGE", "freqtrade-openclaw-liquidity-scalper:local")


def freqtrade_cmd_prefix() -> list[str]:
    venv_cmd = ROOT / ".venv" / "bin" / "freqtrade"
    if venv_cmd.is_file():
        return [str(venv_cmd)]
    return [sys.executable, "-m", "freqtrade"]


@dataclass
class BacktestMetrics:
    trades: int
    profit_abs: float
    profit_pct: float
    max_underwater_pct: float


def run_cmd(cmd: list[str], cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, check=check)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def hyperopt_processes_alive(config_name: str, strategy_name: str) -> bool:
    proc = run_cmd(["ps", "aux"], check=True)
    needle = f"freqtrade hyperopt --config {config_name} --strategy {strategy_name}"
    for line in proc.stdout.splitlines():
        if needle in line and "rg -i" not in line:
            return True
    return False


def wait_for_hyperopt_done(config_name: str, strategy_name: str, poll_seconds: int = 30, timeout_hours: int = 18) -> None:
    deadline = time.time() + timeout_hours * 3600
    while time.time() < deadline:
        if not hyperopt_processes_alive(config_name, strategy_name):
            return
        time.sleep(poll_seconds)
    raise TimeoutError("Timed out waiting for hyperopt process to complete.")


def find_latest_result(strategy_name: str) -> Path:
    candidates = sorted(HYPEROPT_RESULTS.glob(f"strategy_{strategy_name}_*.fthypt"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"No .fthypt file found for {strategy_name} in {HYPEROPT_RESULTS}")
    return candidates[-1]


def parse_best_epoch(result_file: Path, min_trades: int) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_loss = float("inf")
    with result_file.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            metrics = row.get("results_metrics") or {}
            trades = int(metrics.get("total_trades") or 0)
            if trades < min_trades:
                continue
            loss = float(row.get("loss") or float("inf"))
            if loss < best_loss:
                best = row
                best_loss = loss

    if best is None:
        raise RuntimeError(f"No viable epoch found with min_trades >= {min_trades}")
    return best


def parse_backtest_metrics(output: str) -> BacktestMetrics:
    if "No trades made." in output:
        return BacktestMetrics(trades=0, profit_abs=0.0, profit_pct=0.0, max_underwater_pct=0.0)

    def grab(pattern: str, cast):
        m = re.search(pattern, output, re.MULTILINE)
        if not m:
            raise RuntimeError(f"Could not parse backtest output with pattern: {pattern}")
        return cast(m.group(1).replace(",", ""))

    trades = grab(r"Total/Daily Avg Trades\s*│\s*([0-9]+)\s*/", int)
    profit_abs = grab(r"Absolute profit\s*│\s*([-0-9.]+)\s+USDT", float)
    profit_pct = grab(r"Total profit %\s*│\s*([-0-9.]+)%", float)
    max_underwater_pct = grab(r"Max % of account underwater\s*│\s*([-0-9.]+)%", float)
    return BacktestMetrics(
        trades=trades,
        profit_abs=profit_abs,
        profit_pct=profit_pct,
        max_underwater_pct=max_underwater_pct,
    )


def run_backtest(config_path: Path, strategy_name: str, timerange: str, strategy_path: Path | None = None) -> BacktestMetrics:
    cmd = freqtrade_cmd_prefix() + [
        "backtesting",
        "--config",
        str(config_path.relative_to(ROOT)),
        "--strategy",
        strategy_name,
        "--timerange",
        timerange,
        "--cache",
        "none",
        "--export",
        "none",
    ]
    if strategy_path is not None:
        cmd.extend(["--strategy-path", str(strategy_path.relative_to(ROOT))])

    proc = run_cmd(cmd, cwd=ROOT, check=True)
    merged = proc.stdout + "\n" + proc.stderr
    return parse_backtest_metrics(merged)


def build_variant_strategy(base_text: str, variant_class: str, params_details: dict[str, Any]) -> str:
    text = base_text
    text = re.sub(r"class\s+LiquiditySweepScalper\(IStrategy\):", f"class {variant_class}(IStrategy):", text)

    roi = params_details["roi"]
    stoploss = float(params_details["stoploss"]["stoploss"])
    trailing = params_details["trailing"]

    text = re.sub(r"minimal_roi\s*=\s*\{[^\n]*\}", f"minimal_roi = {pformat(roi, sort_dicts=True)}", text)
    text = re.sub(r"stoploss\s*=\s*[-0-9.]+\s*#", f"stoploss = {stoploss}  #", text)
    text = re.sub(r"trailing_stop\s*=\s*(True|False)", f"trailing_stop = {trailing['trailing_stop']}", text)
    text = re.sub(
        r"trailing_stop_positive\s*=\s*[-0-9.]+\s*#",
        f"trailing_stop_positive = {float(trailing['trailing_stop_positive'])}  #",
        text,
    )
    text = re.sub(
        r"trailing_stop_positive_offset\s*=\s*[-0-9.]+\s*#",
        f"trailing_stop_positive_offset = {float(trailing['trailing_stop_positive_offset'])}  #",
        text,
    )
    text = re.sub(
        r"trailing_only_offset_is_reached\s*=\s*(True|False)",
        f"trailing_only_offset_is_reached = {trailing['trailing_only_offset_is_reached']}",
        text,
    )
    return text


def next_free_port(registry: dict[str, Any], start: int = 8117) -> int:
    used = {
        int(bot.get("port"))
        for bot in registry.get("bots", [])
        if bot.get("port") is not None and str(bot.get("port")).isdigit()
    }
    port = start
    while port in used:
        port += 1
    return port


def build_hopt_assets(
    variant_class: str,
    variant_tag: str,
    port: int,
) -> tuple[Path, Path, Path]:
    strategy_path = STRATEGY_DIR / f"{variant_class}.py"
    config_path = USER_DATA / f"config_openclaw-liquidity-scalper-{variant_tag}.json"
    compose_path = ROOT / f"docker-compose.openclaw-liquidity-scalper-{variant_tag}.yml"

    base_cfg = load_json(BASE_CONFIG_PATH)
    base_cfg["strategy"] = variant_class
    base_cfg["bot_name"] = f"openclaw-liquidity-rejection-scalper-{variant_tag}"
    base_cfg.setdefault("api_server", {})["listen_port"] = port
    save_json(config_path, base_cfg)

    compose_text = f"""services:
  freqtrade-openclaw-liquidity-scalper-{variant_tag}:
    build:
      context: .
      dockerfile: Dockerfile
    image: freqtrade-openclaw-liquidity-scalper-{variant_tag}:local
    container_name: freqtrade-openclaw-liquidity-scalper-{variant_tag}
    restart: unless-stopped
    ports:
      - \"{port}:{port}\"
    volumes:
      - ./user_data:/freqtrade/user_data
    command: >
      trade
      --config /freqtrade/user_data/{config_path.name}
      --strategy {variant_class}
      --db-url sqlite:////freqtrade/user_data/trades-openclaw-liquidity-scalper-{variant_tag}.sqlite
"""
    compose_path.write_text(compose_text, encoding="utf-8")
    return strategy_path, config_path, compose_path


def up_container_compose(compose_path: Path) -> None:
    run_cmd(["docker", "compose", "-f", str(compose_path), "up", "-d", "--build"], cwd=ROOT, check=True)


def up_container_docker_run(
    container_name: str,
    config_path: Path,
    strategy_name: str,
    db_name: str,
    port: int,
    runtime_image: str,
) -> None:
    run_cmd(["docker", "rm", "-f", container_name], cwd=ROOT, check=False)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--restart",
        "unless-stopped",
        "-p",
        f"{port}:{port}",
        "-v",
        f"{ROOT / 'user_data'}:/freqtrade/user_data",
        runtime_image,
        "trade",
        "--config",
        f"/freqtrade/user_data/{config_path.name}",
        "--strategy",
        strategy_name,
        "--db-url",
        f"sqlite:////freqtrade/user_data/{db_name}",
    ]
    run_cmd(cmd, cwd=ROOT, check=True)


def update_registry(
    registry_path: Path,
    variant_tag: str,
    variant_class: str,
    config_path: Path,
    port: int,
    baseline: BacktestMetrics,
    candidate: BacktestMetrics,
) -> None:
    registry = load_json(registry_path)
    name = f"freqtrade-openclaw-liquidity-scalper-{variant_tag}"
    bots = registry.setdefault("bots", [])
    bots = [b for b in bots if b.get("name") != name]
    bots.append(
        {
            "name": name,
            "port": port,
            "strategy": variant_class,
            "config": str(config_path.relative_to(ROOT)),
            "mode": "isolated futures dry-run",
            "status": "running",
            "benchmark": (
                f"autopromoted: base {baseline.profit_pct:.2f}% -> "
                f"candidate {candidate.profit_pct:.2f}% (trades {candidate.trades}, dd {candidate.max_underwater_pct:.2f}%)"
            ),
        }
    )
    registry["bots"] = bots
    save_json(registry_path, registry)


def promote_if_improved(
    timerange: str,
    min_trades: int,
    required_positive_profit: bool,
    min_delta_pct: float,
    max_dd_pct: float,
    launch_mode: str,
    runtime_image: str,
) -> int:
    result_file = find_latest_result(BASE_STRATEGY)
    best = parse_best_epoch(result_file=result_file, min_trades=min_trades)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    variant_tag = f"hopt-{ts}"
    variant_class = f"LiquiditySweepScalperHopt{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"

    base_text = BASE_STRATEGY_FILE.read_text(encoding="utf-8")
    variant_text = build_variant_strategy(base_text, variant_class, best["params_details"])

    registry = load_json(REGISTRY_PATH)
    port = next_free_port(registry)
    strategy_path, config_path, compose_path = build_hopt_assets(variant_class, variant_tag, port)
    strategy_path.write_text(variant_text, encoding="utf-8")

    baseline = run_backtest(BASE_CONFIG_PATH, BASE_STRATEGY, timerange)
    candidate = run_backtest(config_path, variant_class, timerange, strategy_path=STRATEGY_DIR)

    delta = candidate.profit_pct - baseline.profit_pct
    improved = delta >= min_delta_pct and candidate.profit_pct > baseline.profit_pct
    if required_positive_profit and candidate.profit_pct <= 0:
        improved = False
    if candidate.max_underwater_pct > max_dd_pct:
        improved = False

    container_name = f"freqtrade-openclaw-liquidity-scalper-{variant_tag}"
    db_name = f"trades-openclaw-liquidity-scalper-{variant_tag}.sqlite"

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "result_file": str(result_file),
        "best_epoch": int(best.get("current_epoch", -1)),
        "best_loss": float(best.get("loss", 0.0)),
        "rules": {
            "min_trades": min_trades,
            "required_positive_profit": required_positive_profit,
            "min_delta_pct": min_delta_pct,
            "max_dd_pct": max_dd_pct,
        },
        "baseline": baseline.__dict__,
        "candidate": candidate.__dict__,
        "delta_profit_pct": delta,
        "variant": {
            "strategy": variant_class,
            "strategy_file": str(strategy_path),
            "config_file": str(config_path),
            "compose_file": str(compose_path),
            "container": container_name,
            "db_name": db_name,
            "port": port,
        },
        "launch_mode": launch_mode,
        "runtime_image": runtime_image,
        "promoted": improved,
    }

    report_path = USER_DATA / "hyperopt_runs" / f"liquidity_autopromote_report_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    save_json(report_path, report)

    if not improved:
        print(f"No promotion: candidate did not satisfy improvement rules. Report: {report_path}")
        return 0

    if launch_mode == "docker-run":
        up_container_docker_run(
            container_name=container_name,
            config_path=config_path,
            strategy_name=variant_class,
            db_name=db_name,
            port=port,
            runtime_image=runtime_image,
        )
    else:
        up_container_compose(compose_path)
    update_registry(REGISTRY_PATH, variant_tag, variant_class, config_path, port, baseline, candidate)
    print(f"Promoted and launched {variant_class} on port {port}. Report: {report_path}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Auto-promote LiquiditySweepScalper hyperopt result if improved.")
    p.add_argument("--timerange", default="20250101-20260520")
    p.add_argument("--min-trades", type=int, default=200)
    p.add_argument("--min-delta-pct", type=float, default=0.10)
    p.add_argument("--max-dd-pct", type=float, default=12.0)
    p.add_argument("--allow-negative-profit", action="store_true")
    p.add_argument("--wait-for-hyperopt", action="store_true")
    p.add_argument("--poll-seconds", type=int, default=30)
    p.add_argument("--timeout-hours", type=int, default=18)
    p.add_argument("--launch-mode", choices=["compose", "docker-run"], default="compose")
    p.add_argument("--runtime-image", default=DEFAULT_RUNTIME_IMAGE)
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.wait_for_hyperopt:
        cfg_rel = str(BASE_CONFIG_PATH.relative_to(ROOT))
        wait_for_hyperopt_done(
            config_name=cfg_rel,
            strategy_name=BASE_STRATEGY,
            poll_seconds=args.poll_seconds,
            timeout_hours=args.timeout_hours,
        )

    return promote_if_improved(
        timerange=args.timerange,
        min_trades=args.min_trades,
        required_positive_profit=not args.allow_negative_profit,
        min_delta_pct=args.min_delta_pct,
        max_dd_pct=args.max_dd_pct,
        launch_mode=args.launch_mode,
        runtime_image=args.runtime_image,
    )


if __name__ == "__main__":
    raise SystemExit(main())
