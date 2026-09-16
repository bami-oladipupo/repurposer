#!/bin/zsh
# Start the Repurposer web UI if it is not running, then open it in the default browser.
ROOT="/Users/bami/repurpose content"
URL="http://127.0.0.1:8080"
if ! curl -fs --max-time 2 "$URL/health" >/dev/null 2>&1; then
  cd "$ROOT" || exit 1
  nohup "$ROOT/.venv/bin/python" app.py >> "$ROOT/logs/web.out.log" 2>&1 &
  for i in {1..20}; do
    sleep 0.5
    curl -fs --max-time 2 "$URL/health" >/dev/null 2>&1 && break
  done
fi
open "$URL"
