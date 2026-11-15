#!/usr/bin/env bash
set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/thumbnail-companion-bridge}"
SERVICE_NAME="thumbnail-companion-bridge.service"

echo "=========================================="
echo " Updating Thumbnail Companion Bridge"
echo "=========================================="

if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR"
else
    DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
    cd "$DIR"
    INSTALL_DIR="$DIR"
fi

echo "Working directory: $INSTALL_DIR"

# 1. Pull latest code if git repo
if [ -d ".git" ]; then
    echo "Pulling latest changes from git..."
    git fetch --all --prune
    git reset --hard origin/$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main") || git pull
else
    echo "Notice: $INSTALL_DIR is not a git repo. Skipping git pull."
fi

chmod +x start.sh update.sh install-lxc.sh 2>/dev/null || true

# 2. Update Python dependencies in virtualenv
if [ -d "venv" ]; then
    echo "Updating Python virtual environment dependencies..."
    ./venv/bin/pip install --upgrade pip --quiet
    if [ -f "requirements.txt" ]; then
        ./venv/bin/pip install -r requirements.txt --quiet
    fi
fi

# 3. Restart systemd service if installed
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}"; then
    echo "Updating unit file and restarting ${SERVICE_NAME}..."
    cp -f "${SERVICE_NAME}" /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart "${SERVICE_NAME}"
    echo "Service status:"
    systemctl --no-pager status "${SERVICE_NAME}"
elif systemctl list-unit-files | grep -q "^automatic-thumbnail\.service"; then
    echo "Restarting legacy automatic-thumbnail.service..."
    systemctl daemon-reload
    systemctl restart automatic-thumbnail.service
    systemctl --no-pager status automatic-thumbnail.service
else
    echo "Notice: ${SERVICE_NAME} not installed in systemd."
fi

echo "=========================================="
echo " Thumbnail Companion Bridge update complete!"
echo "=========================================="
