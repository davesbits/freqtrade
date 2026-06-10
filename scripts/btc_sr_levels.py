#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


@dataclass(frozen=True)
class TfSpec:
    label: str
    resample: str
    swing_order: int


TF_DEFAULTS = [
    TfSpec(label="1M", resample="ME", swing_order=2),
    TfSpec(label="1W", resample="1W-MON", swing_order=3),
    TfSpec(label="1D", resample="1D", swing_order=5),
    TfSpec(label="1H", resample="1h", swing_order=12),
]

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
}

TF_COLOR = {"1M": "magenta", "1W": "cyan", "1D": "blue", "1H": "reset"}


def ctext(text: str, color: str, use_color: bool) -> str:
    if not use_color:
        return text
    prefix = ANSI.get(color, "")
    return f"{prefix}{text}{ANSI['reset']}"


def load_ohlcv(path: Path) -> pd.DataFrame:
    df = pd.read_feather(path)
    req = {"date", "open", "high", "low", "close", "volume"}
    missing = req.difference(df.columns)
    if missing:
        raise ValueError(f"Missing columns in {path}: {sorted(missing)}")
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").set_index("date")
    return df[["open", "high", "low", "close", "volume"]]


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df.resample(rule).agg(agg).dropna()
    return out


def find_swings(df: pd.DataFrame, order: int, timeframe: str) -> list[dict]:
    if len(df) < max(order * 2 + 1, 3):
        return []

    highs_idx = argrelextrema(df["high"].values, np.greater, order=order)[0]
    lows_idx = argrelextrema(df["low"].values, np.less, order=order)[0]
    out: list[dict] = []
    for i in highs_idx:
        out.append(
            {
                "price": float(df["high"].iloc[i]),
                "time": df.index[i],
                "type": "R",
                "volume": float(df["volume"].iloc[i]),
                "timeframe": timeframe,
            }
        )
    for i in lows_idx:
        out.append(
            {
                "price": float(df["low"].iloc[i]),
                "time": df.index[i],
                "type": "S",
                "volume": float(df["volume"].iloc[i]),
                "timeframe": timeframe,
            }
        )
    return out


def cluster_swings(swings: list[dict], tolerance_pct: float) -> list[dict]:
    if not swings:
        return []

    swings_sorted = sorted(swings, key=lambda x: x["price"])
    used = [False] * len(swings_sorted)
    zones: list[dict] = []

    for i in range(len(swings_sorted)):
        if used[i]:
            continue

        members = [swings_sorted[i]]
        used[i] = True
        changed = True

        while changed:
            changed = False
            center = float(np.mean([m["price"] for m in members]))
            for j in range(i + 1, len(swings_sorted)):
                if used[j]:
                    continue
                pct = abs(swings_sorted[j]["price"] - center) / center
                if pct <= tolerance_pct:
                    members.append(swings_sorted[j])
                    used[j] = True
                    changed = True

        prices = [m["price"] for m in members]
        times = [m["time"] for m in members]
        vols = [m["volume"] for m in members]
        s_count = sum(1 for m in members if m["type"] == "S")
        r_count = sum(1 for m in members if m["type"] == "R")
        if abs(s_count - r_count) <= 1:
            bias = "S/R"
        elif s_count > r_count:
            bias = "S"
        else:
            bias = "R"

        zone = {
            "price_center": float(np.mean(prices)),
            "price_low": float(np.min(prices)),
            "price_high": float(np.max(prices)),
            "touches": len(members),
            "support_touches": s_count,
            "resistance_touches": r_count,
            "bias": bias,
            "first_touch": min(times),
            "last_touch": max(times),
            "volume_total": float(np.sum(vols)),
            "volume_avg": float(np.mean(vols)),
        }
        zones.append(zone)

    zones.sort(key=lambda z: z["price_center"])
    return zones


def nearest_levels(zones: list[dict], price: float, count: int) -> list[dict]:
    ranked = sorted(zones, key=lambda z: abs(z["price_center"] - price))
    return ranked[:count]


def fmt_price(x: float) -> str:
    return f"${x:,.0f}"


def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def bias_color(bias: str) -> str:
    if bias == "S":
        return "green"
    if bias == "R":
        return "red"
    return "yellow"


def print_nearest(tf: str, levels: list[dict], current: float, use_color: bool) -> None:
    tf_head = ctext(tf, TF_COLOR.get(tf, "reset"), use_color)
    print(f"\n{tf_head} nearest levels:")
    for z in levels:
        dist_pct = (z["price_center"] / current - 1.0) * 100.0
        bias_txt = ctext(z["bias"], bias_color(z["bias"]), use_color)
        last = pd.Timestamp(z["last_touch"]).strftime("%Y-%m-%d")
        print(
            f"  {fmt_price(z['price_center']):>10}  {bias_txt:>3}  "
            f"{z['touches']:>3} touches  {fmt_pct(dist_pct):>8}  last {last}  "
            f"vol/touch {z['volume_avg']:.2f}"
        )


def print_top(tf: str, levels: list[dict], top_n: int, use_color: bool) -> None:
    tf_head = ctext(tf, TF_COLOR.get(tf, "reset"), use_color)
    print(f"\n{tf_head} strongest levels (top {top_n} by touches):")
    sorted_levels = sorted(levels, key=lambda z: (-z["touches"], z["price_center"]))[:top_n]
    for z in sorted_levels:
        bias_txt = ctext(z["bias"], bias_color(z["bias"]), use_color)
        first = pd.Timestamp(z["first_touch"]).strftime("%Y-%m-%d")
        last = pd.Timestamp(z["last_touch"]).strftime("%Y-%m-%d")
        print(
            f"  {fmt_price(z['price_center']):>10}  {bias_txt:>3}  "
            f"{z['touches']:>3} touches  range {fmt_price(z['price_low'])}-{fmt_price(z['price_high'])}  "
            f"{first} -> {last}"
        )


def to_records(tf: str, levels: list[dict], tf_avg_volume: float, current_price: float) -> list[dict]:
    out = []
    for z in levels:
        dist_pct = (z["price_center"] / current_price - 1.0) * 100.0
        out.append(
            {
                "timeframe": tf,
                "price_center": z["price_center"],
                "price_low": z["price_low"],
                "price_high": z["price_high"],
                "bias": z["bias"],
                "touches": z["touches"],
                "support_touches": z["support_touches"],
                "resistance_touches": z["resistance_touches"],
                "first_touch": pd.Timestamp(z["first_touch"]).isoformat(),
                "last_touch": pd.Timestamp(z["last_touch"]).isoformat(),
                "volume_total_at_touches": z["volume_total"],
                "volume_avg_at_touch": z["volume_avg"],
                "touch_volume_vs_tf_avg": (
                    (z["volume_avg"] / tf_avg_volume) if tf_avg_volume > 0 else np.nan
                ),
                "distance_from_current_pct": dist_pct,
            }
        )
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Color-coded BTC support/resistance levels across 1M/1W/1D/1H with touches and volume."
    )
    parser.add_argument(
        "--input",
        default="user_data/data/binance/BTC_USDT-1h.feather",
        help="Input OHLCV feather file (default: user_data/data/binance/BTC_USDT-1h.feather)",
    )
    parser.add_argument("--tolerance", type=float, default=0.005, help="Clustering tolerance (default: 0.005 = 0.5%%)")
    parser.add_argument("--swing-order-1h", type=int, default=12)
    parser.add_argument("--swing-order-1d", type=int, default=5)
    parser.add_argument("--swing-order-1w", type=int, default=3)
    parser.add_argument("--swing-order-1m", type=int, default=2)
    parser.add_argument("--nearest", type=int, default=8, help="Nearest levels per timeframe")
    parser.add_argument("--top", type=int, default=12, help="Top levels per timeframe by touches")
    parser.add_argument("--outdir", default="user_data/analysis", help="Output directory")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    use_color = not args.no_color
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    base = load_ohlcv(input_path)
    current_price = float(base["close"].iloc[-1])

    tf_specs = [
        TfSpec(label="1M", resample="ME", swing_order=args.swing_order_1m),
        TfSpec(label="1W", resample="1W-MON", swing_order=args.swing_order_1w),
        TfSpec(label="1D", resample="1D", swing_order=args.swing_order_1d),
        TfSpec(label="1H", resample="1h", swing_order=args.swing_order_1h),
    ]

    summary: dict[str, dict] = {}
    all_rows: list[dict] = []

    print(ctext("BTC Binance Support/Resistance Analysis", "bold", use_color))
    print(f"Input: {input_path}")
    print(f"Range: {base.index.min()} -> {base.index.max()}")
    print(f"Current BTC close (1h): {fmt_price(current_price)}")
    print(f"Cluster tolerance: {args.tolerance:.4f} ({args.tolerance * 100:.2f}%)")

    for spec in tf_specs:
        tf_df = resample_ohlcv(base, spec.resample)
        swings = find_swings(tf_df, spec.swing_order, spec.label)
        zones = cluster_swings(swings, args.tolerance)
        nearest = nearest_levels(zones, current_price, args.nearest)
        tf_avg_volume = float(tf_df["volume"].mean()) if len(tf_df) else np.nan

        print_nearest(spec.label, nearest, current_price, use_color)
        print_top(spec.label, zones, args.top, use_color)

        rows = to_records(spec.label, zones, tf_avg_volume=tf_avg_volume, current_price=current_price)
        all_rows.extend(rows)
        summary[spec.label] = {
            "candles": int(len(tf_df)),
            "swings": int(len(swings)),
            "zones": int(len(zones)),
            "support_swings": int(sum(1 for s in swings if s["type"] == "S")),
            "resistance_swings": int(sum(1 for s in swings if s["type"] == "R")),
            "swing_order": int(spec.swing_order),
            "resample": spec.resample,
        }

    rows_df = pd.DataFrame(all_rows).sort_values(["timeframe", "touches", "price_center"], ascending=[True, False, True])
    csv_path = outdir / "btc_sr_levels_1m_1w_1d_1h.csv"
    rows_df.to_csv(csv_path, index=False)

    summary_payload = {
        "input": str(input_path),
        "current_price": current_price,
        "tolerance": args.tolerance,
        "timeframes": summary,
        "generated_at_utc": pd.Timestamp.now("UTC").isoformat(),
        "csv_output": str(csv_path),
    }
    summary_path = outdir / "btc_sr_summary_1m_1w_1d_1h.json"
    summary_path.write_text(json.dumps(summary_payload, indent=2))

    print("\nOutputs:")
    print(f"  CSV:  {csv_path}")
    print(f"  JSON: {summary_path}")


if __name__ == "__main__":
    main()
