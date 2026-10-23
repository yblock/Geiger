#!/bin/bash
# Double-click in Finder to install what Geiger needs and open the dashboard.
# Close this window, or press Ctrl-C, to stop the logger.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${GEIGER_HTTP_PORT:-8320}"
URL="http://localhost:$PORT"

dashboard_up() {
  curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1
}

if dashboard_up; then
  echo "Geiger is already running. Opening $URL"
  open "$URL"
  exit 0
fi

# where uv's installer and Homebrew put things, in case this shell's PATH lacks them
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "Geiger uses uv (https://docs.astral.sh/uv/) to install Python and its packages."
  read -r -p "Install uv now? [y/N] " answer
  case "$answer" in
    [yY]*)
      if command -v brew >/dev/null 2>&1; then
        brew install uv
      else
        curl -LsSf https://astral.sh/uv/install.sh | sh
      fi
      ;;
    *)
      echo "uv not installed; see README.md for setting up by hand."
      exit 1
      ;;
  esac
fi

echo "Installing dependencies..."
uv sync --quiet

# open the dashboard as soon as the server answers
(
  for _ in $(seq 1 120); do
    if dashboard_up; then
      open "$URL"
      exit 0
    fi
    sleep 0.5
  done
) &

echo "Starting Geiger at $URL"
echo "Close this window or press Ctrl-C to stop."
exec uv run geiger run --http-port "$PORT" "$@"
