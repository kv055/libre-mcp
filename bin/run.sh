#!/bin/bash
# Run the libre-mcp stdio server — the command .mcp.json points at.
# stdout is the JSON-RPC channel; env setup is delegated to env.sh (stdout-clean).
#
#   bin/run.sh           plain run (INFO logging, live window by default)
#   bin/run.sh --dev     debug logging + kept profile + hot-reload of uno_worker.py
#   bin/run.sh --live    explicit live window (default; kept for compatibility)
#   bin/run.sh --headless  no window even with a display (CI)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEV=0
LIVE=0
HEADLESS=0
for arg in "$@"; do
  [ "$arg" = "--dev" ] && DEV=1
  [ "$arg" = "--live" ] && LIVE=1
  [ "$arg" = "--headless" ] && HEADLESS=1
done

if [ "$DEV" = "1" ]; then
  # DEBUG log level is what turns on hot-reload in the server.
  export LIBRE_MCP_LOG_LEVEL="${LIBRE_MCP_LOG_LEVEL:-DEBUG}"
  export LIBRE_MCP_KEEP_PROFILE="${LIBRE_MCP_KEEP_PROFILE:-1}"
fi
if [ "$LIVE" = "1" ]; then
  export LIBRE_MCP_LIVE=1
fi
if [ "$HEADLESS" = "1" ]; then
  export LIBRE_MCP_LIVE=0
fi

source "$SCRIPT_DIR/env.sh"

if [ "$DEV" = "1" ] || [ "$LIVE" = "1" ]; then
  {
    echo "libre-mcp dev server"
    echo "  branch:  $LIBRE_MCP_BRANCH"
    echo "  profile: $LIBRE_MCP_PROFILE_DIR"
    echo "  log:     $LIBRE_MCP_LOG_LEVEL"
    [ "$DEV" = "1" ] && echo "  reload:  on (debug; watching uno_worker.py)"
    [ "$LIVE" = "1" ] && echo "  live:    on (visible LibreOffice window)"
  } >&2
fi

exec uv run libre-mcp
