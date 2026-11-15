#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install -r requirements.txt
fi

export OAUTHLIB_INSECURE_TRANSPORT="${OAUTHLIB_INSECURE_TRANSPORT:-1}"

if [ -z "${PORT:-}" ]; then
    CONFIG_FILE="${CONFIG_PATH:-$DIR/config.json}"
    if [ -f "$CONFIG_FILE" ]; then
        PORT=$(./venv/bin/python -c "import json; print(json.load(open('$CONFIG_FILE', encoding='utf-8')).get('port', 5001))" 2>/dev/null || echo "5001")
    else
        PORT="5001"
    fi
fi

echo "Starting Thumbnail Companion Bridge on http://0.0.0.0:${PORT}..."
exec ./venv/bin/gunicorn -w 1 --threads 4 --timeout 120 -b "0.0.0.0:${PORT}" app:app
