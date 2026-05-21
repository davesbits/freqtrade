#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import re
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
REGISTRY_PATH = USER_DATA / "bots_registry.json"
ALLOWED_PAIRS = {"BTC/USDT", "ETH/USDT", "BTC/USDT:USDT", "ETH/USDT:USDT"}
PYTHON_BIN = ROOT / ".venv" / "bin" / "python"


@dataclass
class BotCandidate:
    name: str
    config_path: Path
    strategy: str
    timeframe: str
    mode: str
    stake_currency: str
    pairs: list[str]
    source: str


@dataclass
class BacktestMetrics:
    profit_pct: float
    absolute_profit: float
    trades: int
    winrate_pct: float
    max_drawdown_pct: float
    sharpe: float
    status: str
    raw: dict[str, Any]


@dataclass
class BacktestRun:
    bot: BotCandidate
    strategy: str
    strategy_path: Path | None
    run_label: str
    output_dir: Path
    command: list[str]
    returncode: int
    metrics: BacktestMetrics | None
    result_zip: Path | None
    stderr_tail: str


@dataclass
class StrategyContext:
    strategy_file: Path
    params: dict[str, Any]
    bounds: dict[str, dict[str, Any]]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def normalize_pair(pair: str) -> str:
    text = pair.strip().upper().replace("_", "/")
    return text


def fmt_pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.2f}%"


def fmt_money(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.6f}"


def md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def recent_12m_timerange() -> str:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=365)
    return f"{start.strftime('%Y%m%d')}-{today.strftime('%Y%m%d')}"


def running_freqtrade_containers() -> set[str]:
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return set()

    names = set()
    for line in proc.stdout.splitlines():
        name = line.strip()
        if name.startswith("freqtrade"):
            names.add(name)
    return names


def parse_registry() -> dict[str, dict[str, Any]]:
    if not REGISTRY_PATH.is_file():
        return {}
    data = load_json(REGISTRY_PATH)
    by_name: dict[str, dict[str, Any]] = {}
    for bot in data.get("bots", []):
        name = bot.get("name")
        if name:
            by_name[name] = bot
    return by_name


def normalize_config_path(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    text = str(path_str).strip()
    text = text.replace("/freqtrade/", "")
    text = text.lstrip("/")
    return ROOT / text


def parse_compose_metadata() -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    compose_files = sorted(ROOT.glob("docker-compose*.yml"))
    for compose_file in compose_files:
        lines = compose_file.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            m = re.search(r"container_name:\s*([^\s]+)", line)
            if not m:
                continue
            container = m.group(1).strip()
            block = "\n".join(lines[idx : idx + 60])
            c = re.search(r"--config\s+([^\s]+)", block)
            s = re.search(r"--strategy\s+([^\s]+)", block)
            if not c and not s:
                continue
            info = metadata.setdefault(container, {})
            if c:
                info["config"] = c.group(1)
            if s:
                info["strategy"] = s.group(1)
    return metadata


def extract_mode(cfg: dict[str, Any], registry_mode: str | None) -> str:
    mode = (cfg.get("trading_mode") or "").lower()
    if mode == "futures":
        margin = (cfg.get("margin_mode") or "").lower()
        return f"futures:{margin or 'unknown'}"
    if registry_mode:
        rm = registry_mode.lower()
        if "futures" in rm:
            return "futures:unknown"
    return "spot"


def is_btc_eth_usdt_bot(cfg: dict[str, Any]) -> bool:
    stake = str(cfg.get("stake_currency", "")).upper()
    if stake != "USDT":
        return False
    pairs = cfg.get("exchange", {}).get("pair_whitelist", [])
    if not isinstance(pairs, list) or not pairs:
        return False
    normalized = [normalize_pair(p) for p in pairs]
    return all(p in ALLOWED_PAIRS for p in normalized)


def discover_bots() -> list[BotCandidate]:
    running = running_freqtrade_containers()
    registry = parse_registry()
    compose_meta = parse_compose_metadata()

    bots: list[BotCandidate] = []
    for name in sorted(running):
        reg = registry.get(name, {})
        reg_config = normalize_config_path(reg.get("config")) if reg else None
        reg_strategy = reg.get("strategy") if reg else None

        meta = compose_meta.get(name, {})
        meta_config = normalize_config_path(meta.get("config"))
        meta_strategy = meta.get("strategy")

        config_path = reg_config or meta_config
        if not config_path or not config_path.is_file():
            continue

        cfg = load_json(config_path)
        if not is_btc_eth_usdt_bot(cfg):
            continue

        strategy = str(cfg.get("strategy") or reg_strategy or meta_strategy or "").strip()
        if not strategy:
            continue

        pairs = [normalize_pair(p) for p in cfg.get("exchange", {}).get("pair_whitelist", [])]
        mode = extract_mode(cfg, reg.get("mode") if reg else None)
        timeframe = str(cfg.get("timeframe") or "")

        source_bits = []
        if reg:
            source_bits.append("registry")
        if meta:
            source_bits.append("compose")
        if not source_bits:
            source_bits.append("runtime")

        bots.append(
            BotCandidate(
                name=name,
                config_path=config_path,
                strategy=strategy,
                timeframe=timeframe,
                mode=mode,
                stake_currency=str(cfg.get("stake_currency", "USDT")),
                pairs=pairs,
                source="+".join(source_bits),
            )
        )

    return bots


def run_command(command: list[str], log_path: Path) -> tuple[int, str, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(command, capture_output=True, text=True)
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        handle.write(proc.stdout)
        if proc.stderr:
            handle.write("\n[stderr]\n")
            handle.write(proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def find_newest_result_zip(result_dir: Path, known: set[Path]) -> Path | None:
    zips = sorted(result_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    for z in reversed(zips):
        if z not in known:
            return z
    return None


def _to_float(value: Any, default: float = float("nan")) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_metrics_from_zip(zip_path: Path, strategy_name: str) -> BacktestMetrics:
    with zipfile.ZipFile(zip_path) as archive:
        json_names = [
            n
            for n in archive.namelist()
            if n.endswith(".json") and "_config" not in n and not n.endswith(".meta.json")
        ]
        if not json_names:
            raise ValueError(f"No backtest result json found in {zip_path}")

        payload = json.loads(archive.read(json_names[0]).decode("utf-8"))

    rows = payload.get("strategy_comparison") or []
    row = None
    for item in rows:
        if item.get("key") == strategy_name:
            row = item
            break
    if row is None and rows:
        row = rows[0]

    if row is None:
        strategy_block = payload.get("strategy", {}).get(strategy_name)
        if strategy_block:
            row = {
                "profit_total": strategy_block.get("profit_total"),
                "profit_total_pct": strategy_block.get("profit_total_pct"),
                "profit_total_abs": strategy_block.get("profit_total_abs"),
                "trades": strategy_block.get("total_trades"),
                "wins": strategy_block.get("wins"),
                "losses": strategy_block.get("losses"),
                "draws": strategy_block.get("draws"),
                "winrate": strategy_block.get("winrate"),
                "max_drawdown_account": strategy_block.get("max_drawdown_account"),
                "sharpe": strategy_block.get("sharpe"),
            }

    if row is None:
        raise ValueError(f"No strategy result row found for {strategy_name}")

    profit_pct = _to_float(row.get("profit_total_pct"))
    if math.isnan(profit_pct):
        profit_pct = _to_float(row.get("profit_total"), 0.0) * 100.0

    absolute_profit = _to_float(row.get("profit_total_abs"), 0.0)
    trades = _to_int(row.get("trades"), 0)
    winrate = _to_float(row.get("winrate"), float("nan"))
    if not math.isnan(winrate) and winrate <= 1.0:
        winrate *= 100.0
    dd = _to_float(row.get("max_drawdown_account"), float("nan"))
    if not math.isnan(dd) and dd <= 1.0:
        dd *= 100.0

    sharpe = _to_float(row.get("sharpe"), float("nan"))

    return BacktestMetrics(
        profit_pct=profit_pct,
        absolute_profit=absolute_profit,
        trades=trades,
        winrate_pct=winrate,
        max_drawdown_pct=dd,
        sharpe=sharpe,
        status="ok",
        raw=row,
    )


def run_backtest(
    bot: BotCandidate,
    timerange: str,
    output_dir: Path,
    run_label: str,
    strategy_name: str | None = None,
    strategy_path: Path | None = None,
    extra_notes: str | None = None,
) -> BacktestRun:
    out_dir = output_dir / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    known = set(out_dir.glob("*.zip"))

    strategy_to_run = strategy_name or bot.strategy

    cmd = [
        str(PYTHON_BIN if PYTHON_BIN.is_file() else Path("python3")),
        "-m",
        "freqtrade",
        "backtesting",
        "--userdir",
        str(USER_DATA),
        "--config",
        str(bot.config_path),
        "--strategy",
        strategy_to_run,
        "--timerange",
        timerange,
        "--export",
        "trades",
        "--cache",
        "none",
        "--backtest-directory",
        str(out_dir),
    ]
    if strategy_path is not None:
        cmd.extend(["--strategy-path", str(strategy_path), "--recursive-strategy-search"])
    if extra_notes:
        cmd.extend(["--notes", extra_notes])

    log_path = output_dir / "logs" / f"{run_label}.log"
    rc, _stdout, stderr = run_command(cmd, log_path)

    result_zip: Path | None = None
    metrics: BacktestMetrics | None = None
    errtail = "\n".join(stderr.splitlines()[-15:]).strip()

    if rc == 0:
        result_zip = find_newest_result_zip(out_dir, known)
        if result_zip is None:
            rc = 2
            errtail = "Backtest completed but no new result zip was produced"
        else:
            try:
                metrics = parse_metrics_from_zip(result_zip, strategy_to_run)
            except Exception as exc:
                rc = 3
                errtail = str(exc)

    if rc != 0 and metrics is None:
        metrics = BacktestMetrics(
            profit_pct=float("nan"),
            absolute_profit=float("nan"),
            trades=0,
            winrate_pct=float("nan"),
            max_drawdown_pct=float("nan"),
            sharpe=float("nan"),
            status="failed",
            raw={"error": errtail},
        )

    return BacktestRun(
        bot=bot,
        strategy=strategy_to_run,
        strategy_path=strategy_path,
        run_label=run_label,
        output_dir=output_dir,
        command=cmd,
        returncode=rc,
        metrics=metrics,
        result_zip=result_zip,
        stderr_tail=errtail,
    )


def rank_runs(runs: list[BacktestRun], dd_cap_pct: float) -> list[tuple[BacktestRun, str]]:
    table: list[tuple[BacktestRun, str]] = []
    qualified: list[BacktestRun] = []
    risk_exceeded: list[BacktestRun] = []
    failed: list[BacktestRun] = []

    for run in runs:
        metrics = run.metrics
        if run.returncode != 0 or metrics is None or metrics.status != "ok":
            failed.append(run)
            continue
        if math.isnan(metrics.max_drawdown_pct) or metrics.max_drawdown_pct > dd_cap_pct:
            risk_exceeded.append(run)
        else:
            qualified.append(run)

    qualified.sort(
        key=lambda r: (
            r.metrics.profit_pct,
            r.metrics.trades,
            -float("inf") if math.isnan(r.metrics.sharpe) else r.metrics.sharpe,
        ),
        reverse=True,
    )
    risk_exceeded.sort(
        key=lambda r: (
            r.metrics.profit_pct,
            r.metrics.trades,
            -float("inf") if math.isnan(r.metrics.sharpe) else r.metrics.sharpe,
        ),
        reverse=True,
    )

    for run in qualified:
        table.append((run, "qualified"))
    for run in risk_exceeded:
        table.append((run, "risk-exceeded"))
    for run in failed:
        table.append((run, "failed"))

    return table


def write_tournament_summary(
    ranked: list[tuple[BacktestRun, str]],
    out_dir: Path,
    dd_cap_pct: float,
) -> tuple[Path, Path]:
    csv_path = out_dir / "tournament_summary.csv"
    md_path = out_dir / "tournament_summary.md"

    headers = [
        "bot",
        "pair/mode",
        "profit%",
        "absolute profit",
        "trades",
        "winrate",
        "max DD",
        "Sharpe",
        "status",
    ]

    rows: list[list[str]] = []
    for run, status in ranked:
        metrics = run.metrics
        pairs = ",".join(run.bot.pairs)
        pair_mode = f"{pairs} | {run.bot.mode}"
        rows.append(
            [
                run.bot.name,
                pair_mode,
                fmt_pct(metrics.profit_pct),
                fmt_money(metrics.absolute_profit),
                str(metrics.trades),
                fmt_pct(metrics.winrate_pct),
                fmt_pct(metrics.max_drawdown_pct),
                "n/a" if math.isnan(metrics.sharpe) else f"{metrics.sharpe:.3f}",
                status,
            ]
        )

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Backtesting Tournament Summary\n\n")
        handle.write(f"- max drawdown cap: `{dd_cap_pct:.2f}%`\n")
        handle.write(f"- generated at: `{datetime.now(timezone.utc).isoformat()}`\n\n")
        handle.write("| " + " | ".join(headers) + " |\n")
        handle.write("| " + " | ".join("---" for _ in headers) + " |\n")
        for row in rows:
            handle.write("| " + " | ".join(md_escape(cell) for cell in row) + " |\n")

    return csv_path, md_path


def find_strategy_file(strategy_name: str) -> Path:
    direct = USER_DATA / "strategies" / f"{strategy_name}.py"
    if direct.is_file():
        return direct

    for path in (USER_DATA / "strategies").rglob("*.py"):
        if path.stem == strategy_name:
            return path

    raise FileNotFoundError(f"Cannot locate strategy file for {strategy_name}")


def _const(node: ast.AST) -> Any | None:
    if isinstance(node, ast.Constant):
        return node.value
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def extract_bounds(strategy_file: Path) -> dict[str, dict[str, Any]]:
    tree = ast.parse(strategy_file.read_text(encoding="utf-8"), filename=str(strategy_file))
    bounds: dict[str, dict[str, Any]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        call = node.value
        if not isinstance(call, ast.Call):
            continue

        fn_name = ""
        if isinstance(call.func, ast.Name):
            fn_name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            fn_name = call.func.attr
        if not fn_name.endswith("Parameter"):
            continue

        low = _const(call.args[0]) if len(call.args) >= 1 else None
        high = _const(call.args[1]) if len(call.args) >= 2 else None
        decimals = None
        space = None
        default = None

        for kw in call.keywords:
            if kw.arg == "decimals":
                decimals = _const(kw.value)
            elif kw.arg == "space":
                space = _const(kw.value)
            elif kw.arg == "default":
                default = _const(kw.value)

        bounds[name] = {
            "type": fn_name,
            "space": space,
            "low": low,
            "high": high,
            "decimals": decimals,
            "default": default,
        }

    return bounds


def build_params_from_ast(strategy_name: str, strategy_file: Path) -> dict[str, Any]:
    tree = ast.parse(strategy_file.read_text(encoding="utf-8"), filename=str(strategy_file))

    params = {
        "roi": {},
        "stoploss": {},
        "trailing": {
            "trailing_stop": False,
            "trailing_stop_positive": None,
            "trailing_stop_positive_offset": 0.0,
            "trailing_only_offset_is_reached": False,
        },
        "max_open_trades": {},
        "buy": {},
        "sell": {},
        "entry": {},
        "exit": {},
    }

    class_node = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == strategy_name:
            class_node = node
            break

    if class_node is None:
        return params

    for stmt in class_node.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        if not isinstance(stmt.targets[0], ast.Name):
            continue
        name = stmt.targets[0].id

        if name == "minimal_roi":
            value = _const(stmt.value)
            if isinstance(value, dict):
                params["roi"] = value
            continue
        if name == "stoploss":
            value = _const(stmt.value)
            if isinstance(value, (int, float)):
                params["stoploss"]["stoploss"] = float(value)
            continue
        if name in {
            "trailing_stop",
            "trailing_stop_positive",
            "trailing_stop_positive_offset",
            "trailing_only_offset_is_reached",
        }:
            value = _const(stmt.value)
            params["trailing"][name] = value
            continue

        call = stmt.value
        if not isinstance(call, ast.Call):
            continue

        fn_name = ""
        if isinstance(call.func, ast.Name):
            fn_name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            fn_name = call.func.attr
        if not fn_name.endswith("Parameter"):
            continue

        default = None
        space = None
        for kw in call.keywords:
            if kw.arg == "default":
                default = _const(kw.value)
            if kw.arg == "space":
                space = _const(kw.value)

        if default is None or not isinstance(space, str):
            continue

        if space in ("buy", "entry", "sell", "exit"):
            params[space][name] = default

    return params


def load_strategy_context(strategy_name: str) -> StrategyContext:
    strategy_file = find_strategy_file(strategy_name)
    params_file = strategy_file.with_suffix(".json")

    if params_file.is_file():
        data = load_json(params_file)
        params = data.get("params", {}) if isinstance(data, dict) else {}
        if not isinstance(params, dict):
            params = {}
    else:
        params = build_params_from_ast(strategy_name, strategy_file)

    params = deepcopy(params)
    for key in ["roi", "stoploss", "trailing", "buy", "sell", "entry", "exit", "max_open_trades"]:
        params.setdefault(key, {} if key != "trailing" else {
            "trailing_stop": False,
            "trailing_stop_positive": None,
            "trailing_stop_positive_offset": 0.0,
            "trailing_only_offset_is_reached": False,
        })

    bounds = extract_bounds(strategy_file)
    return StrategyContext(strategy_file=strategy_file, params=params, bounds=bounds)


def numeric_paths(params: dict[str, Any], section_names: tuple[str, ...]) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    for section in section_names:
        block = params.get(section, {})
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                paths.append((section, key))
    return paths


def clamp_value(name: str, value: float, bounds: dict[str, dict[str, Any]], orig: Any) -> Any:
    b = bounds.get(name)
    if b:
        low = b.get("low")
        high = b.get("high")
        if isinstance(low, (int, float)):
            value = max(value, float(low))
        if isinstance(high, (int, float)):
            value = min(value, float(high))

        decimals = b.get("decimals")
        if b.get("type") == "IntParameter" or isinstance(orig, int):
            return int(round(value))
        if isinstance(decimals, int):
            return round(value, decimals)
        return float(value)

    if isinstance(orig, int):
        return int(round(value))
    return round(float(value), 6)


def mutate_numeric_value(name: str, value: Any, direction: int, intensity: float, bounds: dict[str, dict[str, Any]]) -> Any:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value

    if isinstance(value, int):
        delta = max(1, int(round(abs(value) * 0.08 * intensity)))
    else:
        base = abs(float(value))
        delta = max(base * 0.08 * intensity, 0.0005)

    mutated = float(value) + (float(direction) * float(delta))

    if name == "stoploss":
        mutated = min(mutated, -0.01)
        mutated = max(mutated, -0.99)
    if name.startswith("trailing_stop_positive") or name.startswith("target_"):
        mutated = max(mutated, 0.0)

    return clamp_value(name, mutated, bounds, value)


def apply_mutation(
    base_params: dict[str, Any],
    entry_paths: list[tuple[str, str]],
    exit_paths: list[tuple[str, str]],
    entry_dir: int,
    exit_dir: int,
    intensity: float,
    bounds: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    params = deepcopy(base_params)

    for section, key in entry_paths:
        value = params.get(section, {}).get(key)
        params[section][key] = mutate_numeric_value(key, value, entry_dir, intensity, bounds)

    for section, key in exit_paths:
        value = params.get(section, {}).get(key)
        params[section][key] = mutate_numeric_value(key, value, exit_dir, intensity, bounds)

    return params


def build_round_candidates(
    base_params: dict[str, Any],
    bounds: dict[str, dict[str, Any]],
    round_name: str,
    intensity: float,
) -> list[tuple[str, dict[str, Any]]]:
    entry_paths = numeric_paths(base_params, ("buy", "entry"))[:2]
    exit_paths = numeric_paths(base_params, ("sell", "exit"))[:2]

    if not entry_paths and not exit_paths:
        return []

    if not exit_paths:
        extra = numeric_paths(base_params, ("trailing", "stoploss", "roi"))
        exit_paths = [p for p in extra if p[1] not in {"trailing_stop"}][:2]

    grid = [
        ("entry_lo", -1, 0),
        ("entry_hi", 1, 0),
        ("exit_lo", 0, -1),
        ("exit_hi", 0, 1),
        ("entry_lo_exit_lo", -1, -1),
        ("entry_hi_exit_hi", 1, 1),
        ("entry_lo_exit_hi", -1, 1),
        ("entry_hi_exit_lo", 1, -1),
    ]

    candidates: list[tuple[str, dict[str, Any]]] = []
    for label, edir, xdir in grid:
        if edir != 0 and not entry_paths:
            continue
        if xdir != 0 and not exit_paths:
            continue
        params = apply_mutation(base_params, entry_paths, exit_paths, edir, xdir, intensity, bounds)
        candidates.append((f"{round_name}_{label}", params))

    dedup: dict[str, tuple[str, dict[str, Any]]] = {}
    for label, params in candidates:
        key = json.dumps(params, sort_keys=True)
        dedup[key] = (label, params)

    return list(dedup.values())


def make_variant_strategy(
    base_strategy_name: str,
    base_strategy_file: Path,
    variant_name: str,
    variant_dir: Path,
    params: dict[str, Any],
) -> tuple[Path, Path]:
    variant_dir.mkdir(parents=True, exist_ok=True)
    py_path = variant_dir / f"{variant_name}.py"
    json_path = variant_dir / f"{variant_name}.json"

    module_loader = (
        "import importlib.util\n"
        "from pathlib import Path\n"
        f"_BASE = Path(r\"{str(base_strategy_file)}\")\n"
        "_SPEC = importlib.util.spec_from_file_location(\"base_strategy_mod\", _BASE)\n"
        "_MOD = importlib.util.module_from_spec(_SPEC)\n"
        "assert _SPEC is not None and _SPEC.loader is not None\n"
        "_SPEC.loader.exec_module(_MOD)\n"
        f"BaseStrategy = getattr(_MOD, \"{base_strategy_name}\")\n\n"
        f"class {variant_name}(BaseStrategy):\n"
        "    pass\n"
    )
    py_path.write_text(module_loader, encoding="utf-8")

    payload = {
        "strategy_name": variant_name,
        "params": params,
        "ft_stratparam_v": 1,
        "export_time": datetime.now(timezone.utc).isoformat(),
    }
    save_json(json_path, payload)
    return py_path, json_path


def select_top2(ranked: list[tuple[BacktestRun, str]]) -> list[BacktestRun]:
    qualified = [r for r, s in ranked if s == "qualified"]
    if len(qualified) >= 2:
        return qualified[:2]
    if len(qualified) == 1:
        others = [r for r, s in ranked if s == "risk-exceeded"]
        return qualified + others[:1]
    risk = [r for r, s in ranked if s == "risk-exceeded"]
    return risk[:2]


def metrics_better(candidate: BacktestMetrics, baseline: BacktestMetrics, dd_cap_pct: float) -> bool:
    if math.isnan(candidate.max_drawdown_pct) or candidate.max_drawdown_pct > dd_cap_pct:
        return False
    return candidate.profit_pct > baseline.profit_pct


def write_round_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def write_csv_with_headers(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def approx_equal(a: float, b: float, tol: float = 1e-9) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    return abs(a - b) <= tol


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtesting tournament + fine-tune pipeline")
    parser.add_argument("--timerange", default=recent_12m_timerange())
    parser.add_argument("--dd-cap", type=float, default=1.0, dest="dd_cap")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--max-bots", type=int, default=0)
    parser.add_argument("--skip-finetune", action="store_true")
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir) if args.output_dir else USER_DATA / "backtest_results" / f"tournament_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    bots = discover_bots()
    if args.max_bots > 0:
        bots = bots[: args.max_bots]

    if not bots:
        print("No running BTC/ETH USDT bots discovered from registry+runtime mapping.")
        return 1

    print(f"Discovered {len(bots)} candidate bots")
    print(f"Timerange: {args.timerange}")
    print(f"DD cap: {args.dd_cap:.2f}%")

    catalog_path = out_dir / "bot_catalog.json"
    save_json(
        catalog_path,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "timerange": args.timerange,
            "bots": [
                {
                    "name": b.name,
                    "config": str(b.config_path),
                    "strategy": b.strategy,
                    "timeframe": b.timeframe,
                    "mode": b.mode,
                    "stake_currency": b.stake_currency,
                    "pairs": b.pairs,
                    "source": b.source,
                }
                for b in bots
            ],
        },
    )

    baseline_runs: list[BacktestRun] = []
    for bot in bots:
        label = f"baseline_{bot.name}"
        run_dir = out_dir / "baseline" / bot.name
        print(f"[baseline] {bot.name} ({bot.strategy})")
        run = run_backtest(bot, args.timerange, run_dir, label)
        baseline_runs.append(run)

    ranked = rank_runs(baseline_runs, args.dd_cap)
    csv_path, md_path = write_tournament_summary(ranked, out_dir, args.dd_cap)

    top2 = select_top2(ranked)
    selections_path = out_dir / "selected_top2.json"
    save_json(
        selections_path,
        {
            "selected": [
                {
                    "bot": run.bot.name,
                    "strategy": run.strategy,
                    "profit_pct": run.metrics.profit_pct,
                    "max_drawdown_pct": run.metrics.max_drawdown_pct,
                }
                for run in top2
            ]
        },
    )

    # Baseline validation rerun for selected bots
    baseline_validation_rows: list[dict[str, Any]] = []
    for idx, run in enumerate(top2, start=1):
        label = f"baseline_validation_{idx}_{run.bot.name}"
        rerun_dir = out_dir / "baseline_validation" / run.bot.name
        rerun = run_backtest(run.bot, args.timerange, rerun_dir, label)

        reproducible = (
            rerun.returncode == 0
            and rerun.metrics is not None
            and run.metrics is not None
            and approx_equal(rerun.metrics.profit_pct, run.metrics.profit_pct, 1e-6)
            and approx_equal(rerun.metrics.max_drawdown_pct, run.metrics.max_drawdown_pct, 1e-6)
            and rerun.metrics.trades == run.metrics.trades
        )

        baseline_validation_rows.append(
            {
                "bot": run.bot.name,
                "baseline_profit_pct": run.metrics.profit_pct if run.metrics else float("nan"),
                "rerun_profit_pct": rerun.metrics.profit_pct if rerun.metrics else float("nan"),
                "baseline_dd_pct": run.metrics.max_drawdown_pct if run.metrics else float("nan"),
                "rerun_dd_pct": rerun.metrics.max_drawdown_pct if rerun.metrics else float("nan"),
                "baseline_trades": run.metrics.trades if run.metrics else -1,
                "rerun_trades": rerun.metrics.trades if rerun.metrics else -1,
                "reproducible": reproducible,
            }
        )

    baseline_headers = [
        "bot",
        "baseline_profit_pct",
        "rerun_profit_pct",
        "baseline_dd_pct",
        "rerun_dd_pct",
        "baseline_trades",
        "rerun_trades",
        "reproducible",
    ]
    write_csv_with_headers(out_dir / "baseline_validation.csv", baseline_headers, baseline_validation_rows)

    promotions: dict[str, dict[str, Any]] = {}

    if not args.skip_finetune:
        for rank_idx, base_run in enumerate(top2, start=1):
            profile = "primary" if rank_idx == 1 else "fallback"
            tune_root = out_dir / "tuning" / base_run.bot.name
            tune_root.mkdir(parents=True, exist_ok=True)

            if base_run.returncode != 0 or base_run.metrics is None:
                promotions[profile] = {
                    "bot": base_run.bot.name,
                    "status": "baseline-failed",
                }
                continue

            print(f"[tune-{profile}] {base_run.bot.name} ({base_run.strategy})")
            context = load_strategy_context(base_run.bot.strategy)

            # Round 1
            round1_candidates = build_round_candidates(context.params, context.bounds, "r1", intensity=1.0)
            round1_rows: list[dict[str, Any]] = []
            round1_runs: list[tuple[str, dict[str, Any], BacktestRun]] = []

            for idx, (label, params) in enumerate(round1_candidates, start=1):
                variant_name = f"{base_run.bot.strategy}_TUNE_{profile.upper()}_R1_{idx:02d}"
                variant_dir = tune_root / "variants"
                make_variant_strategy(
                    base_run.bot.strategy,
                    context.strategy_file,
                    variant_name,
                    variant_dir,
                    params,
                )
                run = run_backtest(
                    base_run.bot,
                    args.timerange,
                    tune_root / "round1",
                    f"{base_run.bot.name}_{label}",
                    strategy_name=variant_name,
                    strategy_path=variant_dir,
                    extra_notes=f"{profile} round1 {label}",
                )
                round1_runs.append((variant_name, params, run))
                m = run.metrics
                round1_rows.append(
                    {
                        "variant": variant_name,
                        "label": label,
                        "profit_pct": m.profit_pct if m else float("nan"),
                        "max_dd_pct": m.max_drawdown_pct if m else float("nan"),
                        "trades": m.trades if m else -1,
                        "sharpe": m.sharpe if m else float("nan"),
                        "status": "ok" if run.returncode == 0 else "failed",
                    }
                )

            write_round_csv(tune_root / "round1_results.csv", round1_rows)

            viable_round1 = [
                t
                for t in round1_runs
                if t[2].returncode == 0
                and t[2].metrics is not None
                and not math.isnan(t[2].metrics.max_drawdown_pct)
                and t[2].metrics.max_drawdown_pct <= args.dd_cap
            ]
            if not viable_round1:
                viable_round1 = [t for t in round1_runs if t[2].returncode == 0 and t[2].metrics is not None]

            viable_round1.sort(
                key=lambda t: (
                    t[2].metrics.profit_pct,
                    t[2].metrics.trades,
                    -float("inf") if math.isnan(t[2].metrics.sharpe) else t[2].metrics.sharpe,
                ),
                reverse=True,
            )

            seed_params = context.params
            if viable_round1:
                seed_params = viable_round1[0][1]

            # Round 2
            round2_candidates = build_round_candidates(seed_params, context.bounds, "r2", intensity=0.5)
            round2_rows: list[dict[str, Any]] = []
            round2_runs: list[tuple[str, dict[str, Any], BacktestRun]] = []
            for idx, (label, params) in enumerate(round2_candidates, start=1):
                variant_name = f"{base_run.bot.strategy}_TUNE_{profile.upper()}_R2_{idx:02d}"
                variant_dir = tune_root / "variants"
                make_variant_strategy(
                    base_run.bot.strategy,
                    context.strategy_file,
                    variant_name,
                    variant_dir,
                    params,
                )
                run = run_backtest(
                    base_run.bot,
                    args.timerange,
                    tune_root / "round2",
                    f"{base_run.bot.name}_{label}",
                    strategy_name=variant_name,
                    strategy_path=variant_dir,
                    extra_notes=f"{profile} round2 {label}",
                )
                round2_runs.append((variant_name, params, run))
                m = run.metrics
                round2_rows.append(
                    {
                        "variant": variant_name,
                        "label": label,
                        "profit_pct": m.profit_pct if m else float("nan"),
                        "max_dd_pct": m.max_drawdown_pct if m else float("nan"),
                        "trades": m.trades if m else -1,
                        "sharpe": m.sharpe if m else float("nan"),
                        "status": "ok" if run.returncode == 0 else "failed",
                    }
                )

            write_round_csv(tune_root / "round2_results.csv", round2_rows)

            combined = round1_runs + round2_runs
            candidates_ok = [t for t in combined if t[2].returncode == 0 and t[2].metrics is not None]
            candidates_ok.sort(
                key=lambda t: (
                    t[2].metrics.profit_pct,
                    t[2].metrics.trades,
                    -float("inf") if math.isnan(t[2].metrics.sharpe) else t[2].metrics.sharpe,
                ),
                reverse=True,
            )

            promoted = None
            for variant_name, params, run in candidates_ok:
                if metrics_better(run.metrics, base_run.metrics, args.dd_cap):
                    promoted = (variant_name, params, run)
                    break

            if promoted is None:
                profile_dir = out_dir / f"{profile}_best"
                profile_dir.mkdir(parents=True, exist_ok=True)
                retained_name = f"{base_run.bot.strategy}_BASELINE_{profile.upper()}_BEST"
                make_variant_strategy(
                    base_run.bot.strategy,
                    context.strategy_file,
                    retained_name,
                    profile_dir,
                    context.params,
                )
                retained_cfg = load_json(base_run.bot.config_path)
                retained_cfg["strategy"] = retained_name
                save_json(profile_dir / f"{profile}_best_config.json", retained_cfg)
                save_json(
                    profile_dir / f"{profile}_best_snapshot.json",
                    {
                        "profile": profile,
                        "status": "baseline-retained",
                        "base_bot": base_run.bot.name,
                        "base_strategy": base_run.bot.strategy,
                        "retained_strategy": retained_name,
                        "timerange": args.timerange,
                        "dd_cap_pct": args.dd_cap,
                        "baseline": {
                            "profit_pct": base_run.metrics.profit_pct,
                            "max_drawdown_pct": base_run.metrics.max_drawdown_pct,
                            "trades": base_run.metrics.trades,
                            "sharpe": base_run.metrics.sharpe,
                        },
                    },
                )
                promotions[profile] = {
                    "bot": base_run.bot.name,
                    "baseline_profit_pct": base_run.metrics.profit_pct,
                    "status": "not-promoted",
                    "snapshot_dir": str(profile_dir),
                }
                continue

            variant_name, params, promoted_run = promoted
            variant_dir = tune_root / "variants"
            profile_dir = out_dir / f"{profile}_best"
            profile_dir.mkdir(parents=True, exist_ok=True)

            # Persist explicit best/fallback snapshots.
            make_variant_strategy(
                base_run.bot.strategy,
                context.strategy_file,
                variant_name,
                profile_dir,
                params,
            )

            promoted_cfg = load_json(base_run.bot.config_path)
            promoted_cfg["strategy"] = variant_name
            save_json(profile_dir / f"{profile}_best_config.json", promoted_cfg)
            save_json(
                profile_dir / f"{profile}_best_snapshot.json",
                {
                    "profile": profile,
                    "base_bot": base_run.bot.name,
                    "base_strategy": base_run.bot.strategy,
                    "promoted_strategy": variant_name,
                    "timerange": args.timerange,
                    "dd_cap_pct": args.dd_cap,
                    "baseline": {
                        "profit_pct": base_run.metrics.profit_pct,
                        "max_drawdown_pct": base_run.metrics.max_drawdown_pct,
                        "trades": base_run.metrics.trades,
                        "sharpe": base_run.metrics.sharpe,
                    },
                    "promoted": {
                        "profit_pct": promoted_run.metrics.profit_pct,
                        "max_drawdown_pct": promoted_run.metrics.max_drawdown_pct,
                        "trades": promoted_run.metrics.trades,
                        "sharpe": promoted_run.metrics.sharpe,
                    },
                },
            )

            # Regression rerun for promoted strategy.
            regression_run = run_backtest(
                base_run.bot,
                args.timerange,
                out_dir / "regression" / profile,
                f"regression_{profile}_{base_run.bot.name}",
                strategy_name=variant_name,
                strategy_path=profile_dir,
                extra_notes=f"regression {profile}",
            )

            reproducible = (
                regression_run.returncode == 0
                and regression_run.metrics is not None
                and approx_equal(regression_run.metrics.profit_pct, promoted_run.metrics.profit_pct, 1e-6)
                and approx_equal(regression_run.metrics.max_drawdown_pct, promoted_run.metrics.max_drawdown_pct, 1e-6)
                and regression_run.metrics.trades == promoted_run.metrics.trades
            )

            promotions[profile] = {
                "bot": base_run.bot.name,
                "status": "promoted",
                "baseline_profit_pct": base_run.metrics.profit_pct,
                "promoted_profit_pct": promoted_run.metrics.profit_pct,
                "baseline_max_dd_pct": base_run.metrics.max_drawdown_pct,
                "promoted_max_dd_pct": promoted_run.metrics.max_drawdown_pct,
                "promoted_strategy": variant_name,
                "snapshot_dir": str(profile_dir),
                "regression_reproducible": reproducible,
            }

        if len(top2) < 2 and "fallback" not in promotions:
            promotions["fallback"] = {"status": "unavailable", "reason": "Only one candidate bot selected."}

    save_json(
        out_dir / "promotion_summary.json",
        {
            "timerange": args.timerange,
            "dd_cap_pct": args.dd_cap,
            "promotions": promotions,
            "artifacts": {
                "catalog": str(catalog_path),
                "summary_csv": str(csv_path),
                "summary_md": str(md_path),
                "top2": str(selections_path),
            },
        },
    )

    print(f"Tournament complete: {out_dir}")
    print(f"Summary CSV: {csv_path}")
    print(f"Summary MD:  {md_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
