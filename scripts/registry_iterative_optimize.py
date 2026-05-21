#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOURNAMENT_SCRIPT = ROOT / "scripts" / "backtesting_tournament.py"


def load_tournament_module():
    spec = importlib.util.spec_from_file_location("bt", TOURNAMENT_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {TOURNAMENT_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bt"] = mod
    spec.loader.exec_module(mod)
    return mod


def recent_12m_timerange() -> str:
    today = datetime.now(timezone.utc).date()
    start = today.replace(year=today.year - 1)
    return f"{start.strftime('%Y%m%d')}-{today.strftime('%Y%m%d')}"


def rank_key(run) -> tuple[float, int, float]:
    m = run.metrics
    if run.returncode != 0 or m is None:
        return (float("-inf"), -1, float("-inf"))
    sharpe = float("-inf") if math.isnan(m.sharpe) else m.sharpe
    return (m.profit_pct, m.trades, sharpe)


def better_than(candidate, baseline, dd_cap: float) -> bool:
    if candidate.returncode != 0 or candidate.metrics is None:
        return False
    if math.isnan(candidate.metrics.max_drawdown_pct) or candidate.metrics.max_drawdown_pct > dd_cap:
        return False
    return candidate.metrics.profit_pct > baseline.metrics.profit_pct


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Iterative optimization for running registry bots")
    parser.add_argument("--timerange", default=recent_12m_timerange())
    parser.add_argument("--dd-cap", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--max-bots", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    bt = load_tournament_module()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = (
        Path(args.output_dir)
        if args.output_dir
        else bt.USER_DATA / "backtest_results" / f"registry_iter_opt_{stamp}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    bots = bt.discover_bots()
    if args.max_bots > 0:
        bots = bots[: args.max_bots]
    if not bots:
        print("No running registry bots found.")
        return 1

    print(f"Discovered {len(bots)} running bots")
    print(f"Timerange: {args.timerange}")
    print(f"DD cap: {args.dd_cap:.2f}%")
    print(f"Iterations: {args.iterations}")

    baseline_runs = []
    for bot in bots:
        run = bt.run_backtest(
            bot=bot,
            timerange=args.timerange,
            output_dir=out_dir / "baseline" / bot.name,
            run_label=f"baseline_{bot.name}",
        )
        baseline_runs.append(run)

    ranked = bt.rank_runs(baseline_runs, args.dd_cap)
    bt.write_tournament_summary(ranked, out_dir, args.dd_cap)

    improvements: list[dict[str, Any]] = []

    for base_run, status in ranked:
        if base_run.returncode != 0 or base_run.metrics is None:
            improvements.append(
                {
                    "bot": base_run.bot.name,
                    "baseline_profit_pct": "n/a",
                    "best_profit_pct": "n/a",
                    "delta_pct": "n/a",
                    "baseline_dd_pct": "n/a",
                    "best_dd_pct": "n/a",
                    "promoted": False,
                    "status": "baseline-failed",
                    "best_variant": "",
                }
            )
            continue

        print(f"[optimize] {base_run.bot.name} ({base_run.bot.strategy})")

        ctx = bt.load_strategy_context(base_run.bot.strategy)
        best_run = base_run
        best_params = deepcopy(ctx.params)
        seed_params = deepcopy(ctx.params)

        per_bot_rows = []
        for i in range(1, args.iterations + 1):
            intensity = max(0.25, 1.0 - (i - 1) * 0.25)
            candidates = bt.build_round_candidates(seed_params, ctx.bounds, f"it{i}", intensity=intensity)
            round_runs = []
            for idx, (label, params) in enumerate(candidates, start=1):
                variant_name = f"{base_run.bot.strategy}_ITER_{i:02d}_{idx:02d}"
                variant_dir = out_dir / "tuning" / base_run.bot.name / "variants"
                bt.make_variant_strategy(
                    base_strategy_name=base_run.bot.strategy,
                    base_strategy_file=ctx.strategy_file,
                    variant_name=variant_name,
                    variant_dir=variant_dir,
                    params=params,
                )
                run = bt.run_backtest(
                    bot=base_run.bot,
                    timerange=args.timerange,
                    output_dir=out_dir / "tuning" / base_run.bot.name / f"iter_{i:02d}",
                    run_label=f"{base_run.bot.name}_{variant_name}",
                    strategy_name=variant_name,
                    strategy_path=variant_dir,
                    extra_notes=f"iterative optimization iteration {i}",
                )
                round_runs.append((variant_name, params, run))

                m = run.metrics
                per_bot_rows.append(
                    {
                        "iteration": i,
                        "variant": variant_name,
                        "label": label,
                        "profit_pct": m.profit_pct if m else float("nan"),
                        "max_dd_pct": m.max_drawdown_pct if m else float("nan"),
                        "trades": m.trades if m else -1,
                        "sharpe": m.sharpe if m else float("nan"),
                        "status": "ok" if run.returncode == 0 else "failed",
                    }
                )

            round_runs.sort(key=lambda t: rank_key(t[2]), reverse=True)
            viable = [
                t
                for t in round_runs
                if t[2].returncode == 0
                and t[2].metrics is not None
                and not math.isnan(t[2].metrics.max_drawdown_pct)
                and t[2].metrics.max_drawdown_pct <= args.dd_cap
            ]
            if viable:
                top_variant, top_params, top_run = viable[0]
                seed_params = deepcopy(top_params)
                if better_than(top_run, best_run, args.dd_cap):
                    best_run = top_run
                    best_params = deepcopy(top_params)

        write_csv(out_dir / "tuning" / base_run.bot.name / "iter_results.csv", per_bot_rows)

        promoted = best_run.run_label != base_run.run_label and better_than(best_run, base_run, args.dd_cap)
        profile_dir = out_dir / "best_profiles" / base_run.bot.name
        profile_dir.mkdir(parents=True, exist_ok=True)

        if promoted:
            promoted_name = f"{base_run.bot.strategy}_ITER_BEST"
            bt.make_variant_strategy(
                base_strategy_name=base_run.bot.strategy,
                base_strategy_file=ctx.strategy_file,
                variant_name=promoted_name,
                variant_dir=profile_dir,
                params=best_params,
            )
            snapshot_status = "promoted"
            best_strategy = promoted_name
        else:
            retained_name = f"{base_run.bot.strategy}_BASELINE_BEST"
            bt.make_variant_strategy(
                base_strategy_name=base_run.bot.strategy,
                base_strategy_file=ctx.strategy_file,
                variant_name=retained_name,
                variant_dir=profile_dir,
                params=ctx.params,
            )
            snapshot_status = "baseline-retained"
            best_strategy = retained_name

        cfg = bt.load_json(base_run.bot.config_path)
        cfg["strategy"] = best_strategy
        bt.save_json(profile_dir / "best_config.json", cfg)
        bt.save_json(
            profile_dir / "best_snapshot.json",
            {
                "bot": base_run.bot.name,
                "profile_status": snapshot_status,
                "baseline": {
                    "profit_pct": base_run.metrics.profit_pct,
                    "max_dd_pct": base_run.metrics.max_drawdown_pct,
                    "trades": base_run.metrics.trades,
                    "sharpe": base_run.metrics.sharpe,
                },
                "best": {
                    "profit_pct": best_run.metrics.profit_pct,
                    "max_dd_pct": best_run.metrics.max_drawdown_pct,
                    "trades": best_run.metrics.trades,
                    "sharpe": best_run.metrics.sharpe,
                },
                "strategy": best_strategy,
                "timerange": args.timerange,
                "dd_cap_pct": args.dd_cap,
            },
        )

        delta = best_run.metrics.profit_pct - base_run.metrics.profit_pct
        improvements.append(
            {
                "bot": base_run.bot.name,
                "baseline_profit_pct": round(base_run.metrics.profit_pct, 4),
                "best_profit_pct": round(best_run.metrics.profit_pct, 4),
                "delta_pct": round(delta, 4),
                "baseline_dd_pct": round(base_run.metrics.max_drawdown_pct, 6),
                "best_dd_pct": round(best_run.metrics.max_drawdown_pct, 6),
                "promoted": promoted,
                "status": status,
                "best_variant": best_strategy,
            }
        )

    improvements.sort(
        key=lambda r: (float(r["delta_pct"]) if isinstance(r["delta_pct"], (int, float)) else float("-inf")),
        reverse=True,
    )

    write_csv(out_dir / "optimization_results.csv", improvements)
    bt.save_json(
        out_dir / "optimization_results.json",
        {
            "timerange": args.timerange,
            "dd_cap_pct": args.dd_cap,
            "iterations": args.iterations,
            "results": improvements,
        },
    )

    print(f"Done: {out_dir}")
    print(f"Summary: {out_dir / 'optimization_results.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
