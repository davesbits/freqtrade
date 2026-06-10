#!/usr/bin/env bash
# Host-native DCA Short hyperopt workflow.
# Hyperopts LiquidityRejectionDcaShort for BTC and ETH perpetual futures.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

FR_BIN="$ROOT_DIR/.venv/bin/freqtrade"
PY_BIN="${PY_BIN:-$ROOT_DIR/.venv/bin/python}"
[[ -x "$FR_BIN" ]] || { echo "ERROR: freqtrade not found at $FR_BIN"; exit 1; }
[[ -x "$PY_BIN" ]] || { echo "ERROR: python not found at $PY_BIN"; exit 1; }

TIMERANGE_BTC="${TIMERANGE_BTC:-20250101-20260605}"
TIMERANGE_ETH="${TIMERANGE_ETH:-20250101-20260520}"
EPOCHS="${EPOCHS:-400}"
MIN_TRADES="${MIN_TRADES:-100}"
JOB_WORKERS="${JOB_WORKERS:-1}"
LOSS_FN="${LOSS_FN:-ProfitDrawDownHyperOptLoss}"
MIN_DELTA_PCT="${MIN_DELTA_PCT:-0.25}"
MAX_DD_PCT="${MAX_DD_PCT:-12.0}"

LOCK_DIR="$ROOT_DIR/user_data/.dca_hyperopt_workflow_host.lock"
LOG_DIR="$ROOT_DIR/user_data/hyperopt_runs"
REPORT_DIR="$ROOT_DIR/user_data/reports"
mkdir -p "$LOG_DIR" "$REPORT_DIR"

export PYTHONPATH="$ROOT_DIR:$PYTHONPATH"

run_cycle() {
  local ts
  ts="$(date -u +%Y%m%d-%H%M%S)"
  local cycle_log="$LOG_DIR/dca_hyperopt_workflow_host_${ts}.log"

  {
    echo "[$(date -u +%FT%TZ)] dca_cycle_start"

    # ── BTC DCA Short (buy sell) ──
    echo "[$(date -u +%FT%TZ)] dca_hyperopt_start btc-dca-short"
    "$FR_BIN" hyperopt \
      --config "$ROOT_DIR/user_data/config_openclaw-btc-dca-short.json" \
      --strategy LiquidityRejectionDcaShort \
      --timerange "$TIMERANGE_BTC" \
      --spaces buy sell \
      --epochs "$EPOCHS" \
      --min-trades "$MIN_TRADES" \
      --hyperopt-loss "$LOSS_FN" \
      --random-state 42 \
      --job-workers "$JOB_WORKERS" \
      --disable-param-export
    echo "[$(date -u +%FT%TZ)] dca_hyperopt_done btc-dca-short"

    # ── ETH DCA Short (buy sell) ──
    echo "[$(date -u +%FT%TZ)] dca_hyperopt_start eth-dca-short"
    "$FR_BIN" hyperopt \
      --config "$ROOT_DIR/user_data/config_openclaw-eth-dca-short.json" \
      --strategy LiquidityRejectionDcaShort \
      --timerange "$TIMERANGE_ETH" \
      --spaces buy sell \
      --epochs "$EPOCHS" \
      --min-trades "$MIN_TRADES" \
      --hyperopt-loss "$LOSS_FN" \
      --random-state 42 \
      --job-workers "$JOB_WORKERS" \
      --disable-param-export
    echo "[$(date -u +%FT%TZ)] dca_hyperopt_done eth-dca-short"

    # ── Profit + Health snapshots ──
    "$PY_BIN" "$ROOT_DIR/scripts/freqtrade_profit_report.py" --running-only > "$REPORT_DIR/profit_running_latest.md"
    "$PY_BIN" "$ROOT_DIR/scripts/freqtrade_health_report.py" --running-only > "$REPORT_DIR/health_running_latest.md"

    echo "[$(date -u +%FT%TZ)] dca_cycle_done"
  } 2>&1 | tee "$cycle_log"
}

# ── Run Once or Loop ──────────────────────────────────────────────────────────
RUN_ONCE="${RUN_ONCE:-0}"
CYCLE_SECONDS="${CYCLE_SECONDS:-43200}"  # 12h

while true; do
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap 'rmdir "$LOCK_DIR"' EXIT
    run_cycle
    rmdir "$LOCK_DIR"
    trap - EXIT
  else
    echo "[$(date -u +%FT%TZ)] dca_lock_active skip" | tee -a "$LOG_DIR/dca_hyperopt_workflow_host_lock.log"
  fi

  [[ "$RUN_ONCE" == "1" ]] && break
  sleep "$CYCLE_SECONDS"
done
