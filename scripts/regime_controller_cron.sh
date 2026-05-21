#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_DIR="${ROOT_DIR}/user_data/.regime_controller.lock"
MODE="${1:-dry-run}"

if [[ "${MODE}" != "dry-run" && "${MODE}" != "execute" ]]; then
  echo "Usage: $0 [dry-run|execute]"
  exit 2
fi

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "Lock active: another regime controller run is in progress"
  exit 0
fi
trap 'rmdir "${LOCK_DIR}"' EXIT

cd "${ROOT_DIR}"
.venv/bin/python scripts/regime_controller.py --mode "${MODE}"
