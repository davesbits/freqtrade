#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "user_data" / "regime_spawn_rules.json"
DEFAULT_STATE = ROOT / "user_data" / "regime_controller_state.json"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


@dataclass
class Action:
    kind: str
    container: str
    reason: str
    command: list[str]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.is_file():
        return {} if default is None else default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def run_command(command: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(command, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def list_containers(all_containers: bool) -> set[str]:
    cmd = ["docker", "ps", "--format", "{{.Names}}"]
    if all_containers:
        cmd = ["docker", "ps", "-a", "--format", "{{.Names}}"]
    rc, out, _ = run_command(cmd)
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _close_series(df):
    if "close" in df.columns:
        return df["close"].astype(float)
    return df.iloc[:, 4].astype(float)


def _ts_series(df):
    if "date" in df.columns:
        return pd.to_datetime(df["date"], utc=True)
    if "timestamp" in df.columns:
        ts = df["timestamp"]
        if str(ts.dtype).startswith("datetime"):
            return pd.to_datetime(ts, utc=True)
        return pd.to_datetime(ts, unit="ms", utc=True)
    return pd.to_datetime(df.iloc[:, 0], utc=True)


def compute_market_metrics(data_path: Path) -> dict[str, Any]:
    if pd is None:
        raise RuntimeError("pandas is required. Use .venv python for this script.")
    if not data_path.is_file():
        raise FileNotFoundError(f"Missing market data file: {data_path}")

    df = pd.read_feather(data_path)
    close = _close_series(df).reset_index(drop=True)
    ts = _ts_series(df).reset_index(drop=True)

    def lookback_return(days: int) -> float:
        bars = days * 24
        if len(close) <= bars:
            raise ValueError(f"Not enough data for {days}d lookback")
        return float(close.iloc[-1] / close.iloc[-1 - bars] - 1.0)

    ema_fast = close.ewm(span=48, adjust=False).mean().iloc[-1]
    ema_slow = close.ewm(span=336, adjust=False).mean().iloc[-1]

    return {
        "asof": ts.iloc[-1].isoformat(),
        "ret_30d": lookback_return(30),
        "ret_120d": lookback_return(120),
        "ema_trend": float(ema_fast / ema_slow - 1.0),
    }


def detect_regime(metrics: dict[str, Any], thresholds: dict[str, Any]) -> str:
    r30 = float(metrics["ret_30d"])
    r120 = float(metrics["ret_120d"])
    trend = float(metrics["ema_trend"])

    if r120 <= float(thresholds["bearish_120d_max"]) and r30 <= float(thresholds["bearish_30d_max"]):
        return "bearish"
    if r120 >= float(thresholds["bullish_120d_min"]) and r30 >= float(thresholds["bullish_30d_min"]):
        return "bullish"
    if trend >= float(thresholds.get("bullish_trend_min", 0.0)) and r30 >= 0.0:
        return "bullish"
    if trend <= float(thresholds.get("bearish_trend_max", 0.0)) and r30 <= 0.0:
        return "bearish"
    return "neutral"


def parse_iso_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def confirmation_target_regime(
    detected: str,
    state: dict[str, Any],
    confirmation: dict[str, Any],
    bootstrap_immediate: bool,
) -> tuple[str, bool, dict[str, Any]]:
    current = state.get("current_regime")
    pending = state.get("pending_regime")
    pending_count = int(state.get("pending_count", 0))

    required = int(confirmation.get("required_consecutive_checks", 2))
    cooldown_hours = float(confirmation.get("cooldown_hours", 24.0))

    if current is None:
        state["current_regime"] = detected
        state["pending_regime"] = None
        state["pending_count"] = 0
        if bootstrap_immediate:
            return detected, True, state
        return detected, False, state

    if detected == current:
        state["pending_regime"] = None
        state["pending_count"] = 0
        return current, False, state

    if detected == pending:
        pending_count += 1
    else:
        pending = detected
        pending_count = 1

    state["pending_regime"] = pending
    state["pending_count"] = pending_count

    if pending_count < required:
        return current, False, state

    last_switch_ts = parse_iso_ts(state.get("last_switch_ts"))
    if last_switch_ts is not None:
        elapsed_h = (datetime.now(timezone.utc) - last_switch_ts).total_seconds() / 3600.0
        if elapsed_h < cooldown_hours:
            return current, False, state

    state["current_regime"] = detected
    state["pending_regime"] = None
    state["pending_count"] = 0
    state["last_switch_ts"] = now_iso()
    return detected, True, state


def desired_containers(rules: dict[str, Any], regime: str) -> set[str]:
    always_on = set(rules.get("always_on", []))
    regime_cfg = rules.get("regimes", {}).get(regime, {})
    activate = set(regime_cfg.get("activate", []))
    return always_on | activate


def managed_containers(rules: dict[str, Any]) -> set[str]:
    explicit = set(rules.get("managed_containers", []))
    all_regime = set()
    for cfg in rules.get("regimes", {}).values():
        all_regime.update(cfg.get("activate", []))
    explicit.update(all_regime)
    return explicit


def build_actions(
    rules: dict[str, Any],
    target_regime: str,
    running: set[str],
    existing: set[str],
) -> list[Action]:
    desired = desired_containers(rules, target_regime)
    managed = managed_containers(rules)
    bot_specs = rules.get("bot_specs", {})

    actions: list[Action] = []

    for container in sorted(desired):
        if container in running:
            continue
        spec = bot_specs.get(container, {})
        start_spec = spec.get("start", {})

        if container in existing:
            actions.append(
                Action(
                    kind="start",
                    container=container,
                    reason=f"activate {target_regime}",
                    command=["docker", "start", container],
                )
            )
            continue

        if start_spec.get("type") == "compose":
            compose_file = start_spec.get("file")
            service = start_spec.get("service") or container
            if compose_file:
                cmd = ["docker", "compose", "-f", str(ROOT / compose_file), "up", "-d", service]
                actions.append(
                    Action(
                        kind="compose_up",
                        container=container,
                        reason=f"spawn {target_regime}",
                        command=cmd,
                    )
                )
                continue

        actions.append(
            Action(
                kind="missing",
                container=container,
                reason="container does not exist and no compose start defined",
                command=[],
            )
        )

    for container in sorted((managed & running) - desired):
        spec = bot_specs.get(container, {})
        start_spec = spec.get("start", {})
        if start_spec.get("type") == "compose":
            compose_file = start_spec.get("file")
            service = start_spec.get("service") or container
            if compose_file:
                cmd = ["docker", "compose", "-f", str(ROOT / compose_file), "down", service]
                actions.append(
                    Action(
                        kind="compose_down",
                        container=container,
                        reason=f"deactivate for {target_regime}",
                        command=cmd,
                    )
                )
                continue
        actions.append(
            Action(
                kind="stop",
                container=container,
                reason=f"deactivate for {target_regime}",
                command=["docker", "stop", container],
            )
        )

    return actions


def execute_actions(actions: list[Action], execute: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in actions:
        if not action.command:
            results.append(
                {
                    "container": action.container,
                    "action": action.kind,
                    "status": "skipped",
                    "reason": action.reason,
                }
            )
            continue

        if not execute:
            results.append(
                {
                    "container": action.container,
                    "action": action.kind,
                    "status": "planned",
                    "reason": action.reason,
                    "command": action.command,
                }
            )
            continue

        rc, out, err = run_command(action.command)
        results.append(
            {
                "container": action.container,
                "action": action.kind,
                "status": "ok" if rc == 0 else "error",
                "reason": action.reason,
                "returncode": rc,
                "stdout": out,
                "stderr": err,
                "command": action.command,
            }
        )
    return results


def print_summary(
    mode: str,
    metrics: dict[str, Any],
    detected: str,
    target: str,
    switched: bool,
    results: list[dict[str, Any]],
) -> None:
    print(f"mode: {mode}")
    print(f"asof: {metrics['asof']}")
    print(
        "metrics: "
        f"ret_30d={metrics['ret_30d']*100:.2f}% "
        f"ret_120d={metrics['ret_120d']*100:.2f}% "
        f"ema_trend={metrics['ema_trend']*100:.2f}%"
    )
    print(f"detected_regime: {detected}")
    print(f"target_regime: {target}")
    print(f"switched: {switched}")

    if not results:
        print("actions: none")
        return

    print("actions:")
    for item in results:
        print(
            f"- {item['container']}: {item['action']} -> {item['status']}"
            + (f" ({item.get('reason', '')})" if item.get("reason") else "")
        )


def main() -> int:
    if pd is None and VENV_PYTHON.is_file() and Path(sys.executable) != VENV_PYTHON:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

    parser = argparse.ArgumentParser(description="Regime-based freqtrade bot spawn controller")
    parser.add_argument("--rules", default=str(DEFAULT_RULES))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    parser.add_argument("--force-regime", choices=["bearish", "neutral", "bullish"], default="")
    args = parser.parse_args()

    rules = load_json(Path(args.rules), default={})
    state_path = Path(args.state)
    state = load_json(state_path, default={})

    data_path = ROOT / rules.get("market_data", {}).get(
        "btc_1h_futures", "user_data/data/binance/futures/BTC_USDT_USDT-1h-futures.feather"
    )
    thresholds = rules.get("thresholds", {})
    confirmation = rules.get("confirmation", {})
    bootstrap_immediate = bool(rules.get("bootstrap_immediate", True))

    metrics = compute_market_metrics(data_path)
    detected = args.force_regime or detect_regime(metrics, thresholds)

    execute = args.mode == "execute"
    decision_state = state if execute else deepcopy(state)

    target, switched, decision_state = confirmation_target_regime(
        detected=detected,
        state=decision_state,
        confirmation=confirmation,
        bootstrap_immediate=bootstrap_immediate,
    )

    running = list_containers(all_containers=False)
    existing = list_containers(all_containers=True)
    actions = build_actions(rules, target, running, existing)

    results = execute_actions(actions, execute=execute)

    if execute:
        state = decision_state

    state["last_run_ts"] = now_iso()
    state["last_detected_regime"] = detected
    state["last_target_regime"] = target
    state["last_switched"] = switched
    state["last_metrics"] = metrics
    state["last_mode"] = args.mode
    state["last_actions"] = results
    save_json(state_path, state)

    print_summary(
        mode=args.mode,
        metrics=metrics,
        detected=detected,
        target=target,
        switched=switched,
        results=results,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
