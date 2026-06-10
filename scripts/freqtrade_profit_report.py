#!/usr/bin/env python3
"""
Print a markdown-style profit/performance summary for all known Freqtrade bots.

This report is results-focused. Health checks live in a separate script.
The script:
- reads `user_data/bots_registry.json` for the bot -> config mapping,
- checks which `freqtrade*` containers are actually running,
- logs into each running bot's REST API,
- fetches `/profit` or `/profit_all`,
- fetches `/performance`,
- prints the key P/L and percentage stats in one terminal view,
- keeps going if a bot is stopped or a request fails.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "user_data" / "bots_registry.json"
API_TIMEOUT_SECONDS = float(os.getenv("FREQTRADE_API_TIMEOUT_SECONDS", "3"))


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def running_containers(registry: dict | None = None) -> set[str]:
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        if registry:
            print(
                f"warning: unable to list docker containers ({exc}); using registry status instead",
                file=sys.stderr,
            )
            return {
                bot.get("name")
                for bot in registry.get("bots", [])
                if bot.get("status") == "running" and bot.get("name")
            }
        print(f"error: unable to list docker containers: {exc}", file=sys.stderr)
        return set()

    return {line.strip() for line in result.stdout.splitlines() if line.strip().startswith("freqtrade")}


def auth_header(username: str | None, password: str | None) -> str | None:
    if not username or not password:
        return None
    credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {credentials}"


def http_json(url: str, method: str = "GET", username: str | None = None, password: str | None = None, data=None):
    request = urllib.request.Request(url, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    header = auth_header(username, password)
    if header:
        request.add_header("Authorization", header)
    payload = None if data is None else json.dumps(data).encode("utf-8")
    if payload is not None:
        request.data = payload

    with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bot_summary(base_url: str, trading_mode: str, username: str | None = None, password: str | None = None) -> dict:
    endpoint = "profit_all" if trading_mode == "futures" else "profit"
    summary = http_json(f"{base_url}/{endpoint}", username=username, password=password)
    performance = http_json(f"{base_url}/performance", username=username, password=password)
    return {"summary": summary, "performance": performance, "endpoint": endpoint}


def fmt_money(value, currency: str) -> str:
    try:
        return f"{float(value):,.4f} {currency}"
    except (TypeError, ValueError):
        return f"n/a {currency}"


def fmt_pct(value) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Freqtrade profit/performance report")
    parser.add_argument("--summary-only", action="store_true", help="Skip top-pairs tables.")
    parser.add_argument("--ranked", action="store_true", help="Print one ranked table instead of per-bot sections.")
    parser.add_argument(
        "--running-only",
        action="store_true",
        help="Include only currently running containers.",
    )
    parser.add_argument(
        "--bots",
        default="",
        help="Comma-separated bot names to include.",
    )
    return parser.parse_args()


def md_escape(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|")


def md_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("| " + " | ".join(headers) + " |")
        print("| " + " | ".join("---" for _ in headers) + " |")
        print("| _no data_ |" + " |" * (len(headers) - 1))
        return

    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        print("| " + " | ".join(md_escape(cell) for cell in row) + " |")


def top_pairs(performance: list[dict], quote_currency: str, limit: int = 5) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in sorted(performance, key=lambda row: row.get("profit_pct", float("-inf")), reverse=True)[:limit]:
        rows.append(
            [
                item.get("pair", ""),
                fmt_money(item.get("profit_abs"), quote_currency),
                fmt_pct(item.get("profit_pct")),
                str(item.get("count", "n/a")),
            ]
        )
    return rows


def print_bot_header(name: str, port: int | str, trading_mode: str, strategy: str, endpoint: str, status: str) -> None:
    print(f"## {name}")
    print(f"- port: `{port}`")
    print(f"- mode: `{trading_mode}`")
    print(f"- strategy: `{strategy}`")
    print(f"- status: `{status}`")
    print(f"- endpoint: `/{endpoint}`")
    print()


def print_spot_summary(summary: dict, performance: list[dict], quote_currency: str, summary_only: bool = False) -> None:
    print("### Summary")
    md_table(
        ["Scope", "P/L", "P/L %", "Trades", "Winrate", "Best Pair"],
        [
            [
                "closed",
                fmt_money(summary.get("profit_closed_coin"), quote_currency),
                fmt_pct(summary.get("profit_closed_percent")),
                str(summary.get("closed_trade_count", summary.get("trade_count", "n/a"))),
                fmt_pct(summary.get("winrate", 0)),
                f"{summary.get('best_pair', '')} ({fmt_pct(summary.get('best_pair_profit_ratio', 0))})",
            ],
            [
                "all",
                fmt_money(summary.get("profit_all_coin"), quote_currency),
                fmt_pct(summary.get("profit_all_percent")),
                str(summary.get("trade_count", "n/a")),
                fmt_pct(summary.get("winrate", 0)),
                f"{summary.get('best_pair', '')} ({fmt_pct(summary.get('best_pair_profit_ratio', 0))})",
            ],
        ],
    )
    if not summary_only:
        print()
        print("### Top Pairs")
        md_table(["Pair", "Profit", "Profit %", "Count"], top_pairs(performance, quote_currency))
        print()


def print_futures_summary(summary: dict, performance: list[dict], quote_currency: str, summary_only: bool = False) -> None:
    print("### Summary")
    rows: list[list[str]] = []
    for bucket in ("all", "long", "short"):
        bucket_data = summary.get(bucket, {})
        rows.append(
            [
                bucket,
                fmt_money(bucket_data.get("profit_all_coin"), quote_currency),
                fmt_pct(bucket_data.get("profit_all_percent")),
                fmt_money(bucket_data.get("profit_closed_coin"), quote_currency),
                fmt_pct(bucket_data.get("profit_closed_percent")),
                str(bucket_data.get("trade_count", "n/a")),
                fmt_pct(bucket_data.get("winrate", 0)),
                f"{bucket_data.get('best_pair', '')} ({fmt_pct(bucket_data.get('best_pair_profit_ratio', 0))})",
            ]
        )
    md_table(
        ["Scope", "All P/L", "All %", "Closed P/L", "Closed %", "Trades", "Winrate", "Best Pair"],
        rows,
    )
    if not summary_only:
        print()
        print("### Top Pairs")
        md_table(["Pair", "Profit", "Profit %", "Count"], top_pairs(performance, quote_currency))
        print()


def make_rank_row(
    name: str,
    status: str,
    trading_mode: str,
    quote_currency: str,
    endpoint: str,
    summary: dict | None,
    error: str = "",
) -> list[str]:
    if summary is None:
        all_profit = "n/a"
        all_pct = "n/a"
        trades = "n/a"
        winrate = "n/a"
        best_pair = "n/a"
    elif endpoint == "profit_all":
        bucket = summary.get("all", {})
        all_profit = fmt_money(bucket.get("profit_all_coin"), quote_currency)
        all_pct = fmt_pct(bucket.get("profit_all_percent"))
        trades = str(bucket.get("trade_count", "n/a"))
        winrate = fmt_pct(bucket.get("winrate", 0))
        best_pair = f"{bucket.get('best_pair', '')} ({fmt_pct(bucket.get('best_pair_profit_ratio', 0))})"
    else:
        all_profit = fmt_money(summary.get("profit_all_coin"), quote_currency)
        all_pct = fmt_pct(summary.get("profit_all_percent"))
        trades = str(summary.get("trade_count", "n/a"))
        winrate = fmt_pct(summary.get("winrate", 0))
        best_pair = f"{summary.get('best_pair', '')} ({fmt_pct(summary.get('best_pair_profit_ratio', 0))})"

    return [
        name,
        status,
        trading_mode,
        all_profit,
        all_pct,
        trades,
        winrate,
        best_pair,
        endpoint,
        error,
    ]


def status_sort_key(row: list[str]) -> tuple[int, float, str]:
    status = row[1]
    pct_text = row[4]
    try:
        pct_value = float(pct_text.replace("%", ""))
    except ValueError:
        pct_value = float("-inf")
    rank = 0 if status == "running" else 1 if status == "stopped" else 2
    return (rank, -pct_value, row[0])


def main() -> int:
    args = parse_args()
    summary_only = args.summary_only
    report_mode = "ranked" if args.ranked else "detailed"

    if not REGISTRY_PATH.is_file():
        print(f"error: missing registry file: {REGISTRY_PATH}", file=sys.stderr)
        return 1

    registry = load_json(REGISTRY_PATH)
    running = running_containers(registry)
    bots = registry.get("bots", [])
    if args.bots.strip():
        wanted = {name.strip() for name in args.bots.split(",") if name.strip()}
        bots = [bot for bot in bots if bot.get("name") in wanted]
    if args.running_only:
        bots = [bot for bot in bots if bot.get("name") in running]

    rank_rows: list[list[str]] = []
    for bot in bots:
        name = bot.get("name")
        status = "running" if name in running else str(bot.get("status", "stopped")).lower() or "stopped"

        config_path = ROOT / bot.get("config", "")
        if not config_path.is_file():
            rank_rows.append(
                make_rank_row(
                    name=name,
                    status="error",
                    trading_mode=str(bot.get("mode", "")).lower() or "spot",
                    quote_currency="USDT",
                    endpoint="n/a",
                    summary=None,
                    error=f"missing config {config_path}",
                )
            )
            if report_mode != "ranked":
                print("=" * 80)
                print_bot_header(
                    name=name,
                    port=bot.get("port", "n/a"),
                    trading_mode=str(bot.get("mode", "")).lower() or "spot",
                    strategy=str(bot.get("strategy", "")),
                    endpoint="n/a",
                    status="error",
                )
                print(f"error: missing config {config_path}")
                print()
            continue

        config = load_json(config_path)
        api_server = config.get("api_server", {})
        username = api_server.get("username") or ""
        password = api_server.get("password") or ""
        port = api_server.get("listen_port") or bot.get("port")
        if not port:
            rank_rows.append(
                make_rank_row(
                    name=name,
                    status="error",
                    trading_mode=str(bot.get("mode", "")).lower() or "spot",
                    quote_currency=str(config.get("stake_currency", "USDT")),
                    endpoint="n/a",
                    summary=None,
                    error="missing api_server.listen_port",
                )
            )
            if report_mode != "ranked":
                print("=" * 80)
                print_bot_header(
                    name=name,
                    port="n/a",
                    trading_mode=str(bot.get("mode", "")).lower() or "spot",
                    strategy=str(config.get("strategy", bot.get("strategy", ""))),
                    endpoint="n/a",
                    status="error",
                )
                print("error: missing api_server.listen_port")
                print()
            continue

        trading_mode = bot.get("mode", "").lower()
        if "futures" in trading_mode:
            trading_mode = "futures"
        else:
            trading_mode = "spot"

        quote_currency = config.get("stake_currency", "USDT")
        endpoint = "profit_all" if trading_mode == "futures" else "profit"

        if status != "running":
            rank_rows.append(
                make_rank_row(
                    name=name,
                    status="stopped",
                    trading_mode=trading_mode,
                    quote_currency=quote_currency,
                    endpoint=endpoint,
                    summary=None,
                    error="not running",
                )
            )
            if report_mode != "ranked":
                print("=" * 80)
                print_bot_header(
                    name=name,
                    port=port,
                    trading_mode=trading_mode,
                    strategy=str(config.get("strategy", bot.get("strategy", ""))),
                    endpoint=endpoint,
                    status="stopped",
                )
                print()
            continue

        base_url = f"http://127.0.0.1:{port}/api/v1"

        try:
            payload = fetch_bot_summary(base_url, trading_mode, username=username, password=password)
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code} calling {base_url}: {exc.reason}"
            rank_rows.append(
                make_rank_row(
                    name=name,
                    status="error",
                    trading_mode=trading_mode,
                    quote_currency=quote_currency,
                    endpoint=endpoint,
                    summary=None,
                    error=error,
                )
            )
            if report_mode != "ranked":
                print("=" * 80)
                print_bot_header(
                    name=name,
                    port=port,
                    trading_mode=trading_mode,
                    strategy=str(config.get("strategy", bot.get("strategy", ""))),
                    endpoint=endpoint,
                    status="error",
                )
                print(f"error: {error}")
                print()
            continue
        except Exception as exc:
            error = str(exc)
            rank_rows.append(
                make_rank_row(
                    name=name,
                    status="error",
                    trading_mode=trading_mode,
                    quote_currency=quote_currency,
                    endpoint=endpoint,
                    summary=None,
                    error=error,
                )
            )
            if report_mode != "ranked":
                print("=" * 80)
                print_bot_header(
                    name=name,
                    port=port,
                    trading_mode=trading_mode,
                    strategy=str(config.get("strategy", bot.get("strategy", ""))),
                    endpoint=endpoint,
                    status="error",
                )
                print(f"error: {error}")
                print()
            continue

        summary = payload["summary"]
        performance = payload["performance"]
        rank_rows.append(
            make_rank_row(
                name=name,
                status=status,
                trading_mode=trading_mode,
                quote_currency=quote_currency,
                endpoint=payload["endpoint"],
                summary=summary,
            )
        )

        if report_mode != "ranked":
            print("=" * 80)
            print_bot_header(
                name=name,
                port=port,
                trading_mode=trading_mode,
                strategy=config.get("strategy", bot.get("strategy", "")),
                endpoint=payload["endpoint"],
                status=status,
            )
            if payload["endpoint"] == "profit_all" and isinstance(summary, dict):
                print_futures_summary(summary, performance, quote_currency, summary_only=summary_only)
            else:
                print_spot_summary(summary, performance, quote_currency, summary_only=summary_only)

    if report_mode == "ranked":
        print("## Ranked Bots")
        md_table(
            ["Bot", "Status", "Mode", "P/L", "P/L %", "Trades", "Winrate", "Best Pair", "Endpoint", "Error"],
            sorted(rank_rows, key=status_sort_key),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
