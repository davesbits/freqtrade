#!/usr/bin/env bash
# Host-native ETH hyperopt workflow.
# Hyperopts Journal, Journal-V2, and SR-Trend-15m strategies for ETH/USDT:USDT.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

FR_BIN="$ROOT_DIR/.venv/bin/freqtrade"
PY_BIN="${PY_BIN:-$ROOT_DIR/.venv/bin/python}"
[[ -x "$FR_BIN" ]] || { echo "ERROR: freqtrade not found at $FR_BIN"; exit 1; }
[[ -x "$PY_BIN" ]] || { echo "ERROR: python not found at $PY_BIN"; exit 1; }

TIMERANGE="${TIMERANGE:-20250101-20260520}"
EPOCHS="${EPOCHS:-400}"
MIN_TRADES="${MIN_TRADES:-150}"
JOB_WORKERS="${JOB_WORKERS:-1}"
LOSS_FN="${LOSS_FN:-ProfitDrawDownHyperOptLoss}"
MIN_DELTA_PCT="${MIN_DELTA_PCT:-0.25}"
MAX_DD_PCT="${MAX_DD_PCT:-12.0}"

LOCK_DIR="$ROOT_DIR/user_data/.eth_hyperopt_workflow_host.lock"
LOG_DIR="$ROOT_DIR/user_data/hyperopt_runs"
REPORT_DIR="$ROOT_DIR/user_data/reports"
mkdir -p "$LOG_DIR" "$REPORT_DIR"

export PYTHONPATH="$ROOT_DIR:$PYTHONPATH"

run_eth_hyperopt() {
  local strategy="$1"
  local config="$2"
  local label="$3"
  local spaces="$4"

  echo "[$(date -u +%FT%TZ)] hyperopt_start strategy=$label timerange=$TIMERANGE epochs=$EPOCHS"

  "$FR_BIN" hyperopt \
    --config "$ROOT_DIR/$config" \
    --strategy "$strategy" \
    --timerange "$TIMERANGE" \
    --spaces "$spaces" \
    --epochs "$EPOCHS" \
    --min-trades "$MIN_TRADES" \
    --hyperopt-loss "$LOSS_FN" \
    --random-state 42 \
    --job-workers "$JOB_WORKERS" \
    --disable-param-export

  echo "[$(date -u +%FT%TZ)] hyperopt_done strategy=$label"
}

promote_if_better() {
  local config="$1"
  local strategy="$2"

  "$PY_BIN" "$ROOT_DIR/scripts/liquidity_hyperopt_autopromote.py" \
    --config "$config" \
    --strategy "$strategy" \
    --timerange "$TIMERANGE" \
    --min-trades "$MIN_TRADES" \
    --min-delta-pct "$MIN_DELTA_PCT" \
    --max-dd-pct "$MAX_DD_PCT" \
    --launch-mode compose \
    2>&1 || echo "[$(date -u +%FT%TZ)] autopromote_skip strategy=$strategy"
}

run_cycle() {
  local ts
  ts="$(date -u +%Y%m%d-%H%M%S)"
  local cycle_log="$LOG_DIR/eth_hyperopt_workflow_host_${ts}.log"

  {
    echo "[$(date -u +%FT%TZ)] eth_cycle_start"

    # 1. ETH Journal (roi stoploss trailing)
    run_eth_hyperopt \
      "OpenClawBtcJournal" \
      "user_data/config_openclaw-eth-journal.json" \
      "eth-journal" \
      "roi stoploss trailing"

    # 2. ETH Journal-V2 (trailing)
    run_eth_hyperopt \
      "OpenClawBtcJournalV2" \
      "user_data/config_openclaw-eth-journal-v2.json" \
      "eth-journal-v2" \
      "trailing"

    # 3. ETH SR-Trend-15m (buy sell)
    run_eth_hyperopt \
      "OpenClawBtcSrTrend15m" \
      "user_data/config_openclaw-eth-sr-trend-15m.json" \
      "eth-sr-trend-15m" \
      "buy sell"

    # ── Autopromote: replace running containers if improvements found ──
    promote_if_better "user_data/config_openclaw-eth-journal.json" "OpenClawBtcJournal"
    promote_if_better "user_data/config_openclaw-eth-journal-v2.json" "OpenClawBtcJournalV2"
    promote_if_better "user_data/config_openclaw-eth-sr-trend-15m.json" "OpenClawBtcSrTrend15m"

    # ── Profit + Health snapshots (all running bots incl. ETH) ──
    "$PY_BIN" "$ROOT_DIR/scripts/freqtrade_profit_report.py" --running-only > "$REPORT_DIR/profit_running_latest.md"
    "$PY_BIN" "$ROOT_DIR/scripts/freqtrade_health_report.py" --running-only > "$REPORT_DIR/health_running_latest.md"

    echo "[$(date -u +%FT%TZ)] eth_cycle_done"
  } 2>&1 | tee "$cycle_log"
}

# ── Run Once or Loop ──────────────────────────────────────────────────────────
RUN_ONCE="${RUN_ONCE:-0}"
CYCLE_SECONDS="${CYCLE_SECONDS:-43200}"  # 12h for ETH (offset from liquidity 6h)

while true; do
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap 'rmdir "$LOCK_DIR"' EXIT
    run_cycle
    rmdir "$LOCK_DIR"
    trap - EXIT
  else
    echo "[$(date -u +%FT%TZ)] eth_lock_active skip" | tee -a "$LOG_DIR/eth_hyperopt_workflow_host_lock.log"
  fi

  [[ "$RUN_ONCE" == "1" ]] && break
  sleep "$CYCLE_SECONDS"
done
