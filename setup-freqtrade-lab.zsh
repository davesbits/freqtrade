#!/usr/bin/env zsh

# Helper functions for the local Freqtrade lab.
# Source this file from ~/.zshrc, for example:
#   source /Users/bits/freqtrade/setup-freqtrade-lab.zsh

if [[ -z "${FREQTRADE_LAB_ROOT:-}" ]]; then
  # `${(%):-%N}` resolves to the current file when sourced in zsh.
  export FREQTRADE_LAB_ROOT="${(%):-%N:A:h}"
fi

check_bots() {
  python3 "$FREQTRADE_LAB_ROOT/scripts/freqtrade_profit_report.py" "$@"
}

check_bots_summary() {
  python3 "$FREQTRADE_LAB_ROOT/scripts/freqtrade_profit_report.py" --summary-only "$@"
}

check_bots_ranked() {
  python3 "$FREQTRADE_LAB_ROOT/scripts/freqtrade_profit_report.py" --ranked "$@"
}

alias CB='check_bots'
alias CBS='check_bots_ranked'
