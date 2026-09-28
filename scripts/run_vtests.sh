#!/bin/bash
# Lanza vtests_build1.py con el entorno de ops (tsbotops.lifecycle) sin leer ni copiar su .env:
# systemd lo carga en una unidad transitoria (no reinicia ni recarga nada). Como root.
#   run_vtests.sh --check | run [--cleanup] | voice | cleanup
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HERE/vtests_$(date +%Y%m%d_%H%M%S).log"
systemd-run --quiet --wait --pipe --collect \
  -p EnvironmentFile=/opt/tsbot-ops/.env -p WorkingDirectory=/opt/tsbot-ops \
  -E LD_LIBRARY_PATH=/opt/tsbot-ops/python/lib -E PYTHONPATH=/opt/tsbot-ops -E PYTHONDONTWRITEBYTECODE=1 \
  /opt/tsbot-ops/.venv/bin/python "$HERE/vtests_build1.py" "$@" < /dev/null 2>&1 | tee "$LOG"
chmod 600 "$LOG"
