#!/usr/bin/env python3
"""
Print a markdown-style health summary for all known Freqtrade bots.

The script:
- reads `user_data/bots_registry.json` for the bot -> config mapping,
- checks which `freqtrade*` containers are actually running,
- pings `/ping` and `/health` for running bots,
- keeps going if a bot is stopped or a request fails.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error

from freqtrade_profit_report import (
    REGISTRY_PATH,
    ROOT,
    http_json,
    load_json,
    md_escape,
    md_table,
    running_containers,
)


def fetch_health(base_url: str, username: str | None = None, password: str | None = None) -> dict:
    ping = http_json(f"{base_url}/ping", username=username, password=password)
    health = http_json(f"{base_url}/health", username=username, password=password)
    return {"ping": ping, "health": health}


def health_status_row(
    name: str,
    status: str,
    port,
    ping: str,
    health: str,
    last_process: str,
    last_process_ts: str,
    error: str = "",
) -> list[str]:
    return [name, status, str(port), ping, health, last_process, last_process_ts, error]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Freqtrade health report")
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


def main() -> int:
    args = parse_args()
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

    rows: list[list[str]] = []
    for bot in bots:
        name = bot.get("name", "")
        status = "running" if name in running else str(bot.get("status", "stopped")).lower() or "stopped"
        config_path = ROOT / bot.get("config", "")
        if not config_path.is_file():
            rows.append(
                health_status_row(
                    name=name,
                    status="error",
                    port=bot.get("port", "n/a"),
                    ping="n/a",
                    health="n/a",
                    last_process="-",
                    last_process_ts="-",
                    error=f"missing config {config_path}",
                )
            )
            continue

        config = load_json(config_path)
        api_server = config.get("api_server", {})
        username = api_server.get("username") or ""
        password = api_server.get("password") or ""
        port = api_server.get("listen_port") or bot.get("port") or "n/a"

        if status != "running":
            rows.append(
                health_status_row(
                    name=name,
                    status="stopped",
                    port=port,
                    ping="-",
                    health="-",
                    last_process="-",
                    last_process_ts="-",
                    error="not running",
                )
            )
            continue

        base_url = f"http://127.0.0.1:{port}/api/v1"
        try:
            payload = fetch_health(base_url, username=username, password=password)
            ping = payload.get("ping", {})
            health = payload.get("health", {})
            ping_status = str(ping.get("status", "n/a"))
            last_process = "-" if health.get("last_process") is None else md_escape(health.get("last_process"))
            last_process_ts = "-" if health.get("last_process_ts") is None else md_escape(health.get("last_process_ts"))
            health_state = "ok" if last_process != "-" or last_process_ts != "-" else "idle"
            rows.append(
                health_status_row(
                    name=name,
                    status="running",
                    port=port,
                    ping=ping_status,
                    health=health_state,
                    last_process=last_process,
                    last_process_ts=last_process_ts,
                )
            )
        except urllib.error.HTTPError as exc:
            rows.append(
                health_status_row(
                    name=name,
                    status="error",
                    port=port,
                    ping="error",
                    health="error",
                    last_process="-",
                    last_process_ts="-",
                    error=f"HTTP {exc.code} calling {base_url}: {exc.reason}",
                )
            )
        except Exception as exc:
            rows.append(
                health_status_row(
                    name=name,
                    status="error",
                    port=port,
                    ping="error",
                    health="error",
                    last_process="-",
                    last_process_ts="-",
                    error=str(exc),
                )
            )

    print("## Freqtrade Health")
    md_table(
        ["Bot", "Status", "Port", "Ping", "Health", "Last Process", "Last TS", "Error"],
        rows,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
