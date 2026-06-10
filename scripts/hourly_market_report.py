#!/usr/bin/env python3
"""Hourly BTC+ETH market report for Telegram delivery."""
import json, subprocess, sys
from datetime import datetime, timezone
import urllib.request

def fetch_ticker(symbol):
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())

def main():
    now = datetime.now(timezone.utc)
    now_london = now.strftime("%H:%M")
    now_utc = now.strftime("%Y-%m-%d %H:%M UTC")

    # Fetch market data
    btc = fetch_ticker("BTC")
    eth = fetch_ticker("ETH")

    btc_price = float(btc["lastPrice"])
    btc_chg = float(btc["priceChangePercent"])
    btc_hi = float(btc["highPrice"])
    btc_lo = float(btc["lowPrice"])
    btc_vol = float(btc["volume"])

    eth_price = float(eth["lastPrice"])
    eth_chg = float(eth["priceChangePercent"])
    eth_hi = float(eth["highPrice"])
    eth_lo = float(eth["lowPrice"])
    eth_vol = float(eth["volume"])

    # Fetch profit report
    result = subprocess.run(
        ["python3", "/Users/bits/freqtrade/scripts/freqtrade_profit_report.py",
         "--summary-only", "--running-only"],
        capture_output=True, text=True, timeout=60,
        cwd="/Users/bits/freqtrade"
    )
    profit_out = result.stdout

    # Parse profit lines
    profit_lines = []
    for line in profit_out.splitlines():
        line = line.strip()
        if "all |" in line and "USDT" in line and "Winrate" not in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                profit_lines.append(parts)
        if "bot_name" in line.lower() or "Strategy:" in line:
            profit_lines.append(["", "", line.strip(), ""])

    # Build report
    report = (
        f"📊 Hourly Market Report — {now_utc}\n\n"
        f"*BTC* \\${btc_price:,.0f} ({btc_chg:+.1f}%)\n"
        f"  H: \\${btc_hi:,.0f}  L: \\${btc_lo:,.0f}  Vol: {btc_vol:,.0f} BTC\n\n"
        f"*ETH* \\${eth_price:,.0f} ({eth_chg:+.1f}%)\n"
        f"  H: \\${eth_hi:,.0f}  L: \\${eth_lo:,.0f}  Vol: {eth_vol:,.0f} ETH\n\n"
        f"─── Bot P/L (running only) ───\n"
        f"{profit_out}"
    )

    # Keep it under Telegram's 4096 char limit if needed
    if len(report) > 4000:
        report = report[:3950] + "\n\n…truncated"

    print(report)
    return report

if __name__ == "__main__":
    main()
