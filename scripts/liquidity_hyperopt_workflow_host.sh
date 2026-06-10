#!/usr/bin/env bash
# Host-native liquidity hyperopt workflow (replaces Docker container).
# Runs directly on macOS M2 Studio — no Docker churn for hyperopt.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PY_BIN="${PY_BIN:-$ROOT_DIR/.venv/bin/python}"
[[ -x "$PY_BIN" ]] || { echo "ERROR: venv not found at $PY_BIN"; exit 1; }

FR_BIN="$ROOT_DIR/.venv/bin/freqtrade"
[[ -x "$FR_BIN" ]] || { echo "ERROR: freqtrade not found at $FR_BIN"; exit 1; }

TIMERANGE="${TIMERANGE:-20250101-20260520}"
EPOCHS="${EPOCHS:-500}"
MIN_TRADES="${MIN_TRADES:-200}"
JOB_WORKERS="${JOB_WORKERS:-1}"
LOSS_FN="${LOSS_FN:-ProfitDrawDownHyperOptLoss}"
MIN_DELTA_PCT="${MIN_DELTA_PCT:-0.10}"
MAX_DD_PCT="${MAX_DD_PCT:-12.0}"
ALLOW_NEGATIVE_PROFIT="${ALLOW_NEGATIVE_PROFIT:-0}"

LOCK_DIR="$ROOT_DIR/user_data/.liquidity_hyperopt_workflow_host.lock"
LOG_DIR="$ROOT_DIR/user_data/hyperopt_runs"
REPORT_DIR="$ROOT_DIR/user_data/reports"
mkdir -p "$LOG_DIR" "$REPORT_DIR"

export PYTHONPATH="$ROOT_DIR:$PYTHONPATH"

run_cycle() {
  local ts
  ts="$(date -u +%Y%m%d-%H%M%S)"
  local cycle_log="$LOG_DIR/liquidity_workflow_host_${ts}.log"

  {
    echo "[$(date -u +%FT%TZ)] cycle_start timerange=$TIMERANGE epochs=$EPOCHS workers=$JOB_WORKERS"

    "$FR_BIN" hyperopt \
      --config "$ROOT_DIR/user_data/config_openclaw-liquidity-scalper.json" \
      --strategy LiquiditySweepScalper \
      --timerange "$TIMERANGE" \
      --spaces roi stoploss trailing \
      --epochs "$EPOCHS" \
      --min-trades "$MIN_TRADES" \
      --hyperopt-loss "$LOSS_FN" \
      --random-state 42 \
      --job-workers "$JOB_WORKERS" \
      --disable-param-export

    local promote_extra=()
    [[ "$ALLOW_NEGATIVE_PROFIT" == "1" ]] && promote_extra+=("--allow-negative-profit")

    "$PY_BIN" "$ROOT_DIR/scripts/liquidity_hyperopt_autopromote.py" \
      --timerange "$TIMERANGE" \
      --min-trades "$MIN_TRADES" \
      --min-delta-pct "$MIN_DELTA_PCT" \
      --max-dd-pct "$MAX_DD_PCT" \
      --launch-mode compose \
      "${promote_extra[@]}"

    "$PY_BIN" "$ROOT_DIR/scripts/freqtrade_profit_report.py" --running-only > "$REPORT_DIR/profit_running_latest.md"
    "$PY_BIN" "$ROOT_DIR/scripts/freqtrade_health_report.py" --running-only > "$REPORT_DIR/health_running_latest.md"

    echo "[$(date -u +%FT%TZ)] cycle_done"
  } 2>&1 | tee "$cycle_log"
}

# ── Run Once or Loop ──────────────────────────────────────────────────────────
RUN_ONCE="${RUN_ONCE:-0}"
CYCLE_SECONDS="${CYCLE_SECONDS:-21600}"

while true; do
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap 'rmdir "$LOCK_DIR"' EXIT
    run_cycle
    rmdir "$LOCK_DIR"
    trap - EXIT
  else
    echo "[$(date -u +%FT%TZ)] lock_active skip" | tee -a "$LOG_DIR/liquidity_workflow_host_lock.log"
  fi

  [[ "$RUN_ONCE" == "1" ]] && break
  sleep "$CYCLE_SECONDS"
done