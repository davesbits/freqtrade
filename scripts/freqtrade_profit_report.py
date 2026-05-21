#!/usr/bin/env python3
"""
Print a markdown-style profit/performance summary for all running Freqtrade bots.

The script:
- reads `user_data/bots_registry.json` for the bot -> config mapping,
- checks which `freqtrade*` containers are actually running,
- logs into each bot's REST API,
- fetches `/profit` or `/profit_all`,
- fetches `/performance`,
- prints the key P/L and percentage stats in one terminal view.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "user_data" / "bots_registry.json"


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

    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_health(base_url: str, username: str | None = None, password: str | None = None) -> dict:
    ping = http_json(f"{base_url}/ping", username=username, password=password)
    health = http_json(f"{base_url}/health", username=username, password=password)
    return {"ping": ping, "health": health}


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


def parse_args() -> bool:
    return "--summary-only" in sys.argv[1:]


def parse_report_mode() -> str:
    if "--ranked" in sys.argv[1:]:
        return "ranked"
    return "detailed"


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


def print_bot_header(name: str, port: int, trading_mode: str, strategy: str, endpoint: str) -> None:
    print(f"## {name}")
    print(f"- port: `{port}`")
    print(f"- mode: `{trading_mode}`")
    print(f"- strategy: `{strategy}`")
    print(f"- endpoint: `/{endpoint}`")
    print()


def print_health_section(health: dict) -> None:
    ping = health.get("ping", {})
    last_process = health.get("health", {})
    md_table(
        ["Check", "Status", "Last Process", "Last Process TS"],
        [
            [
                "ping",
                md_escape(ping.get("status", "n/a")),
                "-",
                "-",
            ],
            [
                "health",
                "ok" if last_process.get("last_process") is not None or last_process.get("last_process_ts") is not None else "idle",
                md_escape(last_process.get("last_process", "")) if last_process.get("last_process") is not None else "-",
                md_escape(last_process.get("last_process_ts", "")) if last_process.get("last_process_ts") is not None else "-",
            ],
        ],
    )
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
    trading_mode: str,
    quote_currency: str,
    endpoint: str,
    summary: dict,
) -> list[str]:
    if endpoint == "profit_all":
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
        trading_mode,
        all_profit,
        all_pct,
        trades,
        winrate,
        best_pair,
        endpoint,
    ]


def main() -> int:
    summary_only = parse_args()
    report_mode = parse_report_mode()

    if not REGISTRY_PATH.is_file():
        print(f"error: missing registry file: {REGISTRY_PATH}", file=sys.stderr)
        return 1

    registry = load_json(REGISTRY_PATH)
    running = running_containers(registry)
    bots = registry.get("bots", [])

    if not running:
        print("No running freqtrade containers found.")
        return 0

    matched = 0
    rank_rows: list[list[str]] = []
    for bot in bots:
        name = bot.get("name")
        if name not in running:
            continue

        config_path = ROOT / bot.get("config", "")
        if not config_path.is_file():
            print(f"{name}: missing config {config_path}")
            continue

        config = load_json(config_path)
        api_server = config.get("api_server", {})
        username = api_server.get("username") or ""
        password = api_server.get("password") or ""
        port = api_server.get("listen_port") or bot.get("port")
        if not port:
            print(f"{name}: missing api_server.listen_port")
            continue

        trading_mode = bot.get("mode", "").lower()
        if "futures" in trading_mode:
            trading_mode = "futures"
        else:
            trading_mode = "spot"

        quote_currency = config.get("stake_currency", "USDT")
        base_url = f"http://127.0.0.1:{port}/api/v1"

        try:
            health = fetch_health(base_url, username=username, password=password)
            payload = fetch_bot_summary(base_url, trading_mode, username=username, password=password)
        except urllib.error.HTTPError as exc:
            print(f"{name}: HTTP {exc.code} calling {base_url}: {exc.reason}")
            continue
        except Exception as exc:
            print(f"{name}: {exc}")
            continue

        matched += 1
        summary = payload["summary"]
        performance = payload["performance"]
        rank_rows.append(
            make_rank_row(
                name=name,
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
            )
            print("### Health")
            print_health_section(health)
            if payload["endpoint"] == "profit_all" and isinstance(summary, dict):
                print_futures_summary(summary, performance, quote_currency, summary_only=summary_only)
            else:
                print_spot_summary(summary, performance, quote_currency, summary_only=summary_only)

    if matched == 0:
        print("No matching running freqtrade bots found in the registry.")
        return 1

    if report_mode == "ranked":
        print("## Ranked Bots")
        md_table(
            ["Bot", "Mode", "P/L", "P/L %", "Trades", "Winrate", "Best Pair", "Endpoint"],
            sorted(
                rank_rows,
                key=lambda row: float(row[3].replace("%", "").replace("n/a", "0") or 0),
                reverse=True,
            ),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
