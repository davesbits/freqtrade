#!/usr/bin/env python3
"""
Multi-TF Regime Controller v2.

Detects regimes on 1h, 4h, and 1w BTC futures data independently.
Each detection TF maps to a bot TF:
  1h regime → 15m bots
  4h regime → 1h bots
  1w regime → 1d bots

When a TF confirms a regime change:
  1. Checks strategy_registry_v2.json for matching configs
  2. If configs exist → starts best-evaluated container
  3. If no configs → writes Codex agent task → agent creates strategy,
     backtests, hyperopts → ASKS USER before promoting

Always-on bots run regardless of regime.

Usage:
  .venv/bin/python scripts/regime_controller_v2.py --mode dry-run
  .venv/bin/python scripts/regime_controller_v2.py --mode execute
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except Exception:
    pd = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "user_data" / "regime_spawn_rules_v2.json"
DEFAULT_REGISTRY = ROOT / "user_data" / "strategy_registry_v2.json"
DEFAULT_STATE = ROOT / "user_data" / "regime_controller_v2_state.json"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
CODEX_TASK_DIR = ROOT / "user_data" / "codex_tasks"


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class Action:
    kind: str  # start_compose, stop_compose, need_codex, missing_config
    container: str
    bot_tf: str
    regime: str
    reason: str
    command: list[str] = field(default_factory=list)
    config: str = ""
    strategy: str = ""
    compose_file: str = ""


@dataclass
class TFRegimeState:
    tf: str
    current_regime: str | None = None
    pending_regime: str | None = None
    pending_count: int = 0
    last_switch_ts: str | None = None


# ── Utilities ─────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.is_file():
        return {} if default is None else default
    with path.open("r", encoding="utf-8") as h:
        return json.load(h)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as h:
        json.dump(payload, h, indent=2)
        h.write("\n")


def run_command(command: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(command, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def list_running_containers() -> set[str]:
    rc, out, _ = run_command(["docker", "ps", "--format", "{{.Names}}"])
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def list_all_containers() -> set[str]:
    rc, out, _ = run_command(["docker", "ps", "-a", "--format", "{{.Names}}"])
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


# ── Market Data ───────────────────────────────────────────────────────────────

def _close_series(df):
    if "close" in df.columns:
        return df["close"].astype(float)
    return df.iloc[:, 4].astype(float)


def compute_tf_metrics(data_path: Path) -> dict[str, Any]:
    """Compute regime-detection metrics for a single TF data file."""
    if pd is None:
        raise RuntimeError("pandas is required")
    if not data_path.is_file():
        raise FileNotFoundError(f"Missing data: {data_path}")

    # Support both feather and JSON data files
    if data_path.suffix == ".json":
        df = pd.read_json(data_path)
    else:
        df = pd.read_feather(data_path)
    close = _close_series(df).reset_index(drop=True)

    def lookback_return(days: int) -> float:
        bars_per_day = {1: 24, 4: 6, 168: 1}  # 1h=24, 4h=6, 1w=1  candles/day
        tf_hours = _infer_tf_hours(data_path)
        bars = days * (24 // tf_hours)
        if len(close) <= bars:
            return float(close.iloc[-1] / close.iloc[0] - 1.0)
        return float(close.iloc[-1] / close.iloc[-1 - bars] - 1.0)

    ema_fast = close.ewm(span=48, adjust=False).mean().iloc[-1]
    ema_slow = close.ewm(span=336, adjust=False).mean().iloc[-1]

    return {
        "ret_30d": lookback_return(30),
        "ret_120d": lookback_return(120),
        "ema_trend": float(ema_fast / ema_slow - 1.0),
    }


def _infer_tf_hours(path: Path) -> int:
    """Infer timeframe hours from filename."""
    name = path.name
    if "4h" in name:
        return 4
    if "1w" in name or "1d" in name:
        return 24  # daily candles (24h) used for weekly regime
    return 1  # default 1h


def detect_regime(metrics: dict[str, Any], thresholds: dict[str, Any]) -> str:
    r30 = float(metrics["ret_30d"])
    r120 = float(metrics["ret_120d"])
    trend = float(metrics["ema_trend"])

    if r120 <= float(thresholds["bearish_120d_max"]) and r30 <= float(thresholds["bearish_30d_max"]):
        return "bearish"
    if r120 >= float(thresholds["bullish_120d_min"]) and r30 >= float(thresholds["bullish_30d_min"]):
        return "bullish"
    if trend >= float(thresholds.get("bullish_trend_min", 0.01)) and r30 >= 0.0:
        return "bullish"
    if trend <= float(thresholds.get("bearish_trend_max", -0.01)) and r30 <= 0.0:
        return "bearish"
    return "choppy_ranging"


# ── Confirmation ──────────────────────────────────────────────────────────────

def parse_iso_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def confirm_tf_regime(
    detected: str,
    tf_state: TFRegimeState,
    cooldown_hours: float,
    required_checks: int,
) -> tuple[str, bool, TFRegimeState]:
    """Returns (target_regime, switched, updated_state)."""
    current = tf_state.current_regime
    pending = tf_state.pending_regime
    count = tf_state.pending_count

    # First run — bootstrap
    if current is None:
        tf_state.current_regime = detected
        tf_state.pending_regime = None
        tf_state.pending_count = 0
        return detected, True, tf_state

    # No change
    if detected == current:
        tf_state.pending_regime = None
        tf_state.pending_count = 0
        return current, False, tf_state

    # Track pending
    if detected == pending:
        count += 1
    else:
        pending = detected
        count = 1

    tf_state.pending_regime = pending
    tf_state.pending_count = count

    if count < required_checks:
        return current, False, tf_state

    # Cooldown check
    last_switch = parse_iso_ts(tf_state.last_switch_ts)
    if last_switch is not None:
        elapsed_h = (datetime.now(timezone.utc) - last_switch).total_seconds() / 3600.0
        if elapsed_h < cooldown_hours:
            return current, False, tf_state

    # Confirm switch
    tf_state.current_regime = detected
    tf_state.pending_regime = None
    tf_state.pending_count = 0
    tf_state.last_switch_ts = now_iso()
    return detected, True, tf_state


# ── Strategy Selection ────────────────────────────────────────────────────────

def pick_strategies_for_tf_regime(
    bot_tf: str,
    regime: str,
    registry: dict[str, Any],
    running: set[str],
) -> list[dict[str, Any]]:
    """Pick strategies from registry for a bot-TF + regime combo.
    Returns list of strategy entries sorted best-first (evaluated > profit)."""
    strategies = registry.get("strategies", {}).get(bot_tf, {}).get(regime, [])
    if not strategies:
        return []

    # Filter out already-running
    available = [s for s in strategies if s.get("service", "") not in running]

    # Sort: evaluated first, then by profit
    def sort_key(s):
        ev = 1 if s.get("evaluated") else 0
        profit = s.get("backtest_profit_pct") or 0
        return (-ev, -profit)

    available.sort(key=sort_key)
    return available


def has_strategies_for_tf_regime(
    bot_tf: str,
    regime: str,
    registry: dict[str, Any],
) -> bool:
    strategies = registry.get("strategies", {}).get(bot_tf, {}).get(regime, [])
    return len(strategies) > 0


# ── Codex Task Bridge ─────────────────────────────────────────────────────────

def write_codex_task(
    bot_tf: str,
    regime: str,
    detection_tf: str,
    registry: dict[str, Any],
) -> Path:
    """Write a task file that triggers the Codex strategy creation pipeline.
    The Dash agent picks this up and spawns a Codex 5.3 sub-agent."""
    CODEX_TASK_DIR.mkdir(parents=True, exist_ok=True)

    # Find what kind of strategy is needed
    empty_slots = registry.get("_empty_slots", {}).get("slots", [])
    needs_hint = ""
    for slot in empty_slots:
        if slot.get("bot_tf") == bot_tf and slot.get("regime") == regime:
            needs_hint = slot.get("needs", "")
            break

    task = {
        "ts": now_iso(),
        "status": "pending",
        "bot_tf": bot_tf,
        "regime": regime,
        "detection_tf": detection_tf,
        "task": f"Create a {regime} strategy for {bot_tf} timeframe on {detection_tf} regime detection",
        "hint": needs_hint,
        "pipeline": [
            "write strategy .py file",
            "write config .json file",
            "backtest 20250101-20260520",
            "hyperopt if backtest passes",
            "report results — DO NOT PROMOTE without user approval"
        ],
        "requirements": {
            "min_trades": 30,
            "min_profit_pct": 0.5,
            "max_dd_pct": 15.0,
            "require_approval": True
        }
    }

    task_file = CODEX_TASK_DIR / f"task_{bot_tf}_{regime}_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    save_json(task_file, task)
    return task_file


# ── Build Actions ─────────────────────────────────────────────────────────────

def build_actions(
    tf_changes: list[tuple[str, str, str]],  # (detection_tf, bot_tf, regime)
    registry: dict[str, Any],
    running: set[str],
    existing: set[str],
    rules: dict[str, Any],
) -> list[Action]:
    """Build actions for a set of TF regime changes."""

    # Gather always-on containers
    always_on = [
        b.get("service", "")
        for b in registry.get("always_on_bots", [])
    ]

    actions: list[Action] = []

    # For each TF regime change, pick strategies
    started_services: set[str] = set()
    for detection_tf, bot_tf, regime in tf_changes:
        has_any = has_strategies_for_tf_regime(bot_tf, regime, registry)
        candidates = pick_strategies_for_tf_regime(bot_tf, regime, registry, running)

        if candidates:
            # Pick the best candidate that isn't already running
            best = candidates[0]
            service = best.get("service", "")
            compose_file = best.get("compose_file", "")
            config = best.get("config", "")
            strategy = best.get("strategy", "")

            if compose_file and service not in running and service not in started_services:
                actions.append(Action(
                    kind="start_compose",
                    container=service,
                    bot_tf=bot_tf,
                    regime=regime,
                    reason=f"{detection_tf} regime={regime} → start {service}",
                    command=["docker", "compose", "-f", str(ROOT / compose_file), "up", "-d", service],
                    config=config,
                    strategy=strategy,
                    compose_file=compose_file,
                ))
                started_services.add(service)
            elif service in existing and service not in running and service not in started_services:
                actions.append(Action(
                    kind="start_compose",
                    container=service,
                    bot_tf=bot_tf,
                    regime=regime,
                    reason=f"{detection_tf} regime={regime} → start {service}",
                    command=["docker", "start", service],
                    config=config,
                    strategy=strategy,
                ))
                started_services.add(service)
            elif not compose_file and service not in existing:
                actions.append(Action(
                    kind="missing_config",
                    container=service,
                    bot_tf=bot_tf,
                    regime=regime,
                    reason=f"{service} has no compose file and container doesn't exist",
                    config=config,
                    strategy=strategy,
                ))
        elif not has_any:
            # Truly no strategies for this TF+regime anywhere → trigger Codex pipeline
            actions.append(Action(
                kind="need_codex",
                container="",
                bot_tf=bot_tf,
                regime=regime,
                reason=f"No strategies for {bot_tf}/{regime} — needs Codex agent",
            ))
        # else: has_any=True but all running → silently OK, no action needed

    # Stop containers that are running but no longer match any active regime
    # Build desired services per bot_tf from what would be picked
    active_regime_services: set[str] = set()
    for _, bot_tf, regime in tf_changes:
        for s in pick_strategies_for_tf_regime(bot_tf, regime, registry, set()):
            active_regime_services.add(s.get("service", ""))

    # All currently desired services: always_on + active regime picks
    all_desired = set(always_on) | active_regime_services
    all_registry_services: set[str] = set()

    # All registry services (any regime, any TF) — do NOT stop these
    for tf_data in registry.get("strategies", {}).values():
        for regime_data in tf_data.values():
            for s in regime_data:
                svc = s.get("service", "")
                if svc:
                    all_registry_services.add(svc)
                    all_desired.add(svc)  # all registry entries are desired
    for b in registry.get("always_on_bots", []):
        svc = b.get("service", "")
        if svc:
            all_registry_services.add(svc)
            all_desired.add(svc)

    # Stop managed containers that are running but not in any registry entry
    for svc in sorted(all_registry_services & running - all_desired):
        if svc in always_on:
            continue
        actions.append(Action(
            kind="stop_compose",
            container=svc,
            bot_tf="",
            regime="",
            reason=f"no longer desired for current regimes",
            command=["docker", "stop", svc],
        ))

    return actions


# ── Execute ───────────────────────────────────────────────────────────────────

def execute_actions(actions: list[Action], execute: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in actions:
        if action.kind == "need_codex":
            # Write task file for Dash agent to pick up
            task_file = write_codex_task(
                action.bot_tf, action.regime, "", load_json(DEFAULT_REGISTRY)
            )
            results.append({
                "container": action.bot_tf,
                "action": "need_codex",
                "status": "task_written",
                "reason": action.reason,
                "task_file": str(task_file),
            })
            continue

        if action.kind in ("missing_config",):
            results.append({
                "container": action.container,
                "action": action.kind,
                "status": "skipped",
                "reason": action.reason,
            })
            continue

        if not execute:
            results.append({
                "container": action.container,
                "action": action.kind,
                "status": "planned",
                "reason": action.reason,
                "command": action.command,
            })
            continue

        rc, out, err = run_command(action.command)
        results.append({
            "container": action.container,
            "action": action.kind,
            "status": "ok" if rc == 0 else "error",
            "reason": action.reason,
            "returncode": rc,
            "stdout": out[:200],
            "stderr": err[:200],
            "command": action.command,
        })
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def load_tf_states(state: dict[str, Any]) -> dict[str, TFRegimeState]:
    """Load per-TF regime states from state file."""
    tf_states: dict[str, TFRegimeState] = {}
    for tf in ["1h", "4h", "1w"]:
        tf_data = state.get("tf_states", {}).get(tf, {})
        tf_states[tf] = TFRegimeState(
            tf=tf,
            current_regime=tf_data.get("current_regime"),
            pending_regime=tf_data.get("pending_regime"),
            pending_count=tf_data.get("pending_count", 0),
            last_switch_ts=tf_data.get("last_switch_ts"),
        )
    return tf_states


def save_tf_states(state: dict[str, Any], tf_states: dict[str, TFRegimeState]) -> None:
    """Save per-TF regime states back to state dict."""
    state["tf_states"] = {}
    for tf, ts in tf_states.items():
        state["tf_states"][tf] = {
            "current_regime": ts.current_regime,
            "pending_regime": ts.pending_regime,
            "pending_count": ts.pending_count,
            "last_switch_ts": ts.last_switch_ts,
        }


def print_summary(
    mode: str,
    tf_results: dict[str, dict[str, Any]],
    tf_changes: list[tuple[str, str, str]],
    actions: list[Action],
    results: list[dict[str, Any]],
) -> None:
    print(f"=== Multi-TF Regime Controller v2 ({mode}) ===\n")
    for tf in ["1h", "4h", "1w"]:
        r = tf_results.get(tf, {})
        detected = r.get("detected", "?")
        metrics = r.get("metrics", {})
        print(
            f"  {tf}: regime={detected}"
            f"  ret_30d={metrics.get('ret_30d', 0)*100:.1f}%"
            f"  ret_120d={metrics.get('ret_120d', 0)*100:.1f}%"
            f"  ema_trend={metrics.get('ema_trend', 0)*100:.1f}%"
        )

    if tf_changes:
        print(f"\n  Regime changes confirmed:")
        for dtf, btf, regime in tf_changes:
            print(f"    {dtf} regime → {regime} → triggers {btf} bots")

    if actions:
        print(f"\n  Actions ({len(actions)}):")
        for a in actions:
            print(f"    [{a.kind}] {a.container or a.bot_tf}: {a.reason}")
    else:
        print(f"\n  No actions needed.")

    if results:
        print(f"\n  Results:")
        for r in results:
            status = r.get("status", "?")
            print(f"    {r.get('container', r.get('action'))}: {r.get('action')} → {status}")
            if r.get("task_file"):
                print(f"      task: {r['task_file']}")


def main() -> int:
    if pd is None and VENV_PYTHON.is_file() and Path(sys.executable) != VENV_PYTHON:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

    parser = argparse.ArgumentParser(description="Multi-TF Regime Controller v2")
    parser.add_argument("--rules", default=str(DEFAULT_RULES))
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    args = parser.parse_args()

    rules = load_json(Path(args.rules), default={})
    registry = load_json(Path(args.registry), default={})
    state_path = Path(args.state)
    state = load_json(state_path, default={})

    execute = args.mode == "execute"
    decision_state = state if execute else deepcopy(state)

    # Load per-TF states
    tf_states = load_tf_states(decision_state)

    # Detect regime for each TF
    confirmation = rules.get("confirmation", {})
    required_checks = int(confirmation.get("required_consecutive_checks", 2))
    cooldown_hours = float(confirmation.get("cooldown_hours", 24.0))

    tf_results: dict[str, dict[str, Any]] = {}
    tf_changes: list[tuple[str, str, str]] = []  # (detection_tf, bot_tf, regime)

    for tf_key in ["1h", "4h", "1w"]:
        data_key = f"btc_{tf_key}_futures"
        data_rel = rules.get("market_data", {}).get(
            data_key, f"user_data/data/binance/futures/BTC_USDT_USDT-{tf_key}-futures.feather"
        )
        data_path = ROOT / data_rel

        if not data_path.is_file():
            print(f"WARNING: Missing data file {data_path} — skipping {tf_key}")
            continue

        metrics = compute_tf_metrics(data_path)
        thresholds = rules.get("thresholds", {}).get(tf_key, rules.get("thresholds", {}))
        detected = detect_regime(metrics, thresholds)

        tf_state = tf_states[tf_key]
        target, switched, tf_state = confirm_tf_regime(
            detected=detected,
            tf_state=tf_state,
            cooldown_hours=cooldown_hours,
            required_checks=required_checks,
        )
        tf_states[tf_key] = tf_state

        tf_results[tf_key] = {
            "detected": detected,
            "target": target,
            "switched": switched,
            "metrics": metrics,
        }

        if switched:
            bot_tf = registry.get("tf_map", {}).get(tf_key, {}).get("bot_tf", "15m")
            tf_changes.append((tf_key, bot_tf, target))

    # Build and execute actions
    running = list_running_containers()
    existing = list_all_containers()

    actions = build_actions(tf_changes, registry, running, existing, rules)
    results = execute_actions(actions, execute=execute)

    # Save state
    save_tf_states(decision_state, tf_states)
    decision_state["last_run_ts"] = now_iso()
    decision_state["last_mode"] = args.mode
    decision_state["last_tf_results"] = {
        tf: {"detected": r["detected"], "switched": r["switched"]}
        for tf, r in tf_results.items()
    }
    decision_state["last_actions"] = [
        {"kind": a.kind, "container": a.container, "regime": a.regime, "bot_tf": a.bot_tf}
        for a in actions
    ]
    if execute:
        state = decision_state
        save_json(state_path, state)

    print_summary(args.mode, tf_results, tf_changes, actions, results)

    # Signal to Dash agent if Codex tasks were created
    codex_tasks = [r for r in results if r.get("action") == "need_codex"]
    if codex_tasks:
        print(f"\n⚠️  {len(codex_tasks)} Codex task(s) created — Dash agent should pick these up.")
        print(f"   Tasks dir: {CODEX_TASK_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
