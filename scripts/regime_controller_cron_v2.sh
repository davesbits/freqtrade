#!/usr/bin/env zsh
# Multi-TF Regime Controller v2 — Cron Wrapper
# Detects regimes on 1h/4h/1w independently and triggers per-TF bot actions.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_DIR="${ROOT_DIR}/user_data/.regime_controller_v2.lock"
MODE="${1:-dry-run}"

if [[ "${MODE}" != "dry-run" && "${MODE}" != "execute" ]]; then
  echo "Usage: $0 [dry-run|execute]"
  exit 2
fi

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "[$(date -u +%FT%TZ)] regime_v2_lock_active skip"
  exit 0
fi
trap 'rmdir "${LOCK_DIR}"' EXIT

cd "${ROOT_DIR}"
.venv/bin/python scripts/regime_controller_v2.py --mode "${MODE}"
