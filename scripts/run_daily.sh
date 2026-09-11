#!/usr/bin/env bash
# Run the GrantWatch pipeline once. Meant for launchd or cron; output goes to logs/daily.log.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
exec .venv/bin/python main.py >> logs/daily.log 2>&1
