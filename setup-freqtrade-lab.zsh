#!/usr/bin/env zsh

# Helper functions for the local Freqtrade lab.
# Source this file from ~/.zshrc, for example:
#   source /Users/bits/freqtrade/setup-freqtrade-lab.zsh

export FREQTRADE_LAB_ROOT="/Users/bits/freqtrade"

CB() {
  python3 "$FREQTRADE_LAB_ROOT/scripts/freqtrade_profit_report.py" "$@"
}

CBS() {
  python3 "$FREQTRADE_LAB_ROOT/scripts/freqtrade_profit_report.py" --ranked "$@"
}

CBH() {
  python3 "$FREQTRADE_LAB_ROOT/scripts/freqtrade_health_report.py" "$@"
}

# Backward-compatible names for older shells and docs.
check_bots() {
  CB "$@"
}

check_bots_summary() {
  python3 "$FREQTRADE_LAB_ROOT/scripts/freqtrade_profit_report.py" --summary-only "$@"
}

check_bots_ranked() {
  CBS "$@"
}
