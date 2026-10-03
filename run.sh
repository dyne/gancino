#!/bin/bash
# Resolve the directory this script lives in, regardless of cwd.
# Cron starts in $HOME, so the explicit cd matters.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/publish.py"