#!/bin/bash
# Run the Hermes digest. Intended to be called from cron.
# Logs to ~/logs/digest-YYYY-MM-DD.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$HOME/logs/digest-$(date +%Y-%m-%d).log"

exec >> "$LOG_FILE" 2>&1

echo "=== Hermes Digest started at $(date) ==="
cd "$SCRIPT_DIR"
"$SCRIPT_DIR/venv/bin/python" digest.py "$@"
echo "=== Done at $(date) ==="
