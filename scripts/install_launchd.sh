#!/usr/bin/env bash
# Install (or refresh) the daily launchd job for this checkout on macOS.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$HOME/Library/LaunchAgents/com.grantwatch.daily.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
sed "s#__REPO__#$REPO#g" "$REPO/scripts/com.grantwatch.daily.plist" > "$TARGET"
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"
echo "Installed $TARGET (runs daily at 07:30). Test it now with: launchctl start com.grantwatch.daily"
