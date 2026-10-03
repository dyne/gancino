#!/bin/bash
set -euo pipefail

# Resolve the directory this script lives in, regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Installing from: $SCRIPT_DIR"

# Pin the build backend for coincurve 20.0.0 (fixes Python 3.14 build).
echo 'scikit-build-core<0.10' > build-constraints.txt

# Create venv next to the script.
python3 -m venv venv
./venv/bin/pip install --upgrade pip

# Use the local build constraint. PIP_CONSTRAINT needs an absolute path.
PIP_CONSTRAINT="$SCRIPT_DIR/build-constraints.txt" \
PIP_BUILD_CONSTRAINT="$SCRIPT_DIR/build-constraints.txt" \
    ./venv/bin/pip install -r requirements.txt

# Seed config.toml from the example if it doesn't exist yet.
if [ ! -f config.toml ]; then
    cp config.toml.example config.toml
    chmod 600 config.toml
    echo
    echo "Created config.toml from example — edit it and set your nsec."
fi

chmod +x run.sh publish.py

echo
echo "Installed to $SCRIPT_DIR"
echo
echo "Next steps:"
echo "  1. Edit $SCRIPT_DIR/config.toml and set your nsec."
echo "  2. Test:   $SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/publish.py"
echo "  3. Add cron (every 30 min):"
echo
echo "     crontab -e"
echo
echo "     */30 * * * * $SCRIPT_DIR/run.sh >> $SCRIPT_DIR/cron.log 2>&1"
echo
