#!/bin/bash
# DF-HLM-2 K16 Concurrent-Spawn-Mutex Wrapper [CRUX-MK]

set -euo pipefail

LOCK_DIR="${DF_HLM_2_LOCK_DIR:-/tmp/df-hlm-2.lock}"
LOCK_AGE_LIMIT_S="${DF_HLM_2_LOCK_AGE_LIMIT_S:-21600}"
STOP_FLAG="${DF_HLM_2_STOP_FLAG:-/tmp/df-hlm-2.stop}"

if [ -f "$STOP_FLAG" ]; then
  echo "[K14-STOP] DF-HLM-2 stopped via $STOP_FLAG" >&2
  exit 4
fi

if [ -d "$LOCK_DIR" ]; then
  LOCK_MTIME=$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  LOCK_AGE_S=$(( NOW - LOCK_MTIME ))
  if [ "$LOCK_AGE_S" -gt "$LOCK_AGE_LIMIT_S" ]; then
    echo "[K16] Stale lock detected (age=${LOCK_AGE_S}s > ${LOCK_AGE_LIMIT_S}s), auto-claiming" >&2
    rm -rf "$LOCK_DIR"
  fi
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[K16-VETO] Concurrent DF-HLM-2 instance detected (lock-dir exists at $LOCK_DIR)" >&2
  exit 3
fi

echo "$$" > "$LOCK_DIR/pid"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LOCK_DIR/started_at"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

PYTHON="${DF_HLM_2_PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

DF_HLM_2_LOCK_HELD=true "$PYTHON" -m src.brand_voice_auditor "$@"
