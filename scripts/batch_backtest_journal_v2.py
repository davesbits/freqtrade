#!/usr/bin/env python3
"""Manual batch backtest for OpenClawBtcJournalV2 — bear market 2021-2022"""
import subprocess, json, sys, itertools

CONFIG = "user_data/config_openclaw-btc-journal.json"
STRATEGY = "OpenClawBtcJournalV2"
TIMERANGE = "20211101-20221130"
FREQTRADE = ".venv/bin/freqtrade"

# Key param combinations to test
combos = [
    # (entry_band_pct, tp1_pct, tp2_pct, stop_buffer, swing_15m, swing_1h, cluster_pct)
    # Defaults first
    (0.008, 0.012, 0.025, 0.012, 5, 4, 0.006),
    # Wider entries
    (0.012, 0.012, 0.025, 0.012, 5, 4, 0.006),
    (0.016, 0.012, 0.025, 0.012, 5, 4, 0.006),
    (0.004, 0.012, 0.025, 0.012, 5, 4, 0.006),
    # Different TP
    (0.008, 0.008, 0.020, 0.012, 5, 4, 0.006),
    (0.008, 0.015, 0.030, 0.012, 5, 4, 0.006),
    (0.008, 0.020, 0.040, 0.012, 5, 4, 0.006),
    # Different stop buffer
    (0.008, 0.012, 0.025, 0.008, 5, 4, 0.006),
    (0.008, 0.012, 0.025, 0.020, 5, 4, 0.006),
    # Swing orders
    (0.008, 0.012, 0.025, 0.012, 3, 3, 0.006),
    (0.008, 0.012, 0.025, 0.012, 7, 6, 0.006),
    (0.008, 0.012, 0.025, 0.012, 8, 8, 0.006),
    # Cluster
    (0.008, 0.012, 0.025, 0.012, 5, 4, 0.004),
    (0.008, 0.012, 0.025, 0.012, 5, 4, 0.010),
    (0.008, 0.012, 0.025, 0.012, 5, 4, 0.014),
    # Best combos from initial results (will be filled)
]

results = []
for i, (eb, t1, t2, sb, s15, s1h, cp) in enumerate(combos):
    hyperopt_args = (
        f"--enable-protections none "
        f"--strategy-path user_data/strategies "
    )
    cmd = [
        FREQTRADE, "backtesting",
        "--config", CONFIG,
        "--strategy", STRATEGY,
        "--timerange", TIMERANGE,
        "--enable-protections", "none",
    ]
    
    # Use strategy-override via hyperopt params
    # Actually, use --hyperopt-space override: we can't easily override from CLI.
    # Instead, use strategy parameter file
    
    # Create a temp json with the params
    params = {
        "params": {
            "buy": {
                "entry_band_pct": eb,
                "tp1_pct": t1,
                "tp2_pct": t2,
                "stop_buffer": sb,
                "swing_order_15m": s15,
                "swing_order_1h": s1h,
                "cluster_pct": cp,
                "min_zone_touches": 3,
                "max_trade_bars": 48,
            }
        }
    }
    
    import tempfile, os
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(params, f)
        param_file = f.name
    
    cmd.extend(["--hyperopt-filename", param_file])
    
    label = f"[{i+1}/{len(combos)}] eb={eb:.3f} t1={t1:.3f} t2={t2:.3f} sb={sb:.3f} s15={s15} s1h={s1h} cp={cp:.3f}"
    print(f"\n{'='*60}")
    print(f"📊 {label}")
    
    try:
        result = subprocess.run(
            cmd, cwd="/Users/bits/freqtrade",
            capture_output=True, text=True, timeout=120
        )
        output = result.stdout + result.stderr
        
        # Extract key metrics
        for line in output.split("\n"):
            if "Tot Profit USDT" in line or "Tot Profit %" in line:
                # Find the strategy summary line
                pass
            
        # Parse strategy summary table
        in_summary = False
        for line in output.split("\n"):
            if "STRATEGY SUMMARY" in line:
                in_summary = True
                continue
            if in_summary and "OpenClawBtcJournalV2" in line:
                parts = line.split("│")
                if len(parts) >= 8:
                    trades = parts[2].strip()
                    avg_prof = parts[3].strip()
                    tot_usdt = parts[4].strip()
                    tot_pct = parts[5].strip()
                    duration = parts[6].strip()
                    winloss = parts[7].strip()
                    dd = parts[8].strip() if len(parts) > 8 else ""
                    print(f"  Trades: {trades} | Avg: {avg_prof} | Total: {tot_usdt} ({tot_pct})")
                    print(f"  Win/Loss: {winloss} | DD: {dd}")
                    results.append({
                        "params": f"eb={eb:.3f}_t1={t1:.3f}_t2={t2:.3f}_sb={sb:.3f}_s15={s15}_s1h={s1h}_cp={cp:.3f}",
                        "trades": trades,
                        "total_pct": tot_pct,
                        "total_usdt": tot_usdt,
                        "winloss": winloss,
                        "dd": dd,
                    })
                break
    except subprocess.TimeoutExpired:
        print(f"  ⏱️ TIMEOUT")
    except Exception as e:
        print(f"  ❌ Error: {e}")
    finally:
        os.unlink(param_file)

# Summary
print(f"\n{'='*60}")
print(f"📋 RESULTS RANKED")
print(f"{'='*60}")
results.sort(key=lambda r: float(r['total_pct'].replace('%','').replace(' USDT','').strip()))
for r in results:
    print(f"  {r['total_pct']:>10s} | {r['trades']:>5s} trades | {r['winloss']:>20s} | {r['params']}")
