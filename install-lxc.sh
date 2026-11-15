#!/usr/bin/env bash
set -e

# Thumbnail Companion Bridge - LXC Automated Installer & Update Linker
# Compatible with Debian/Ubuntu LXCs (Proxmox VE / standalone LXC)

if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo bash install-lxc.sh)"
  exit 1
fi

SERVICE_NAME="thumbnail-companion-bridge.service"
TARGET_DIR="/opt/thumbnail-companion-bridge"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

echo "=========================================================="
echo " Installing Thumbnail Companion Bridge in LXC"
echo "=========================================================="

# 1. Install prerequisites
echo "1. Checking system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl fonts-dejavu-core >/dev/null

# 2. Deploy files to target directory
echo "2. Setting up $TARGET_DIR..."
mkdir -p "$TARGET_DIR"

if [ "$SOURCE_DIR" != "$TARGET_DIR" ]; then
    echo "Copying application files to $TARGET_DIR..."
    cp -r "$SOURCE_DIR"/* "$TARGET_DIR"/ 2>/dev/null || true
    if [ -d "$SOURCE_DIR/.git" ]; then
        cp -r "$SOURCE_DIR/.git" "$TARGET_DIR"/ 2>/dev/null || true
    fi
fi

cd "$TARGET_DIR"
mkdir -p "$TARGET_DIR/thumbnails"

# Ensure scripts have execute permission
chmod +x start.sh update.sh 2>/dev/null || true

# 3. Create Virtualenv and install Python dependencies
echo "3. Setting up Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip --quiet
./venv/bin/pip install -r requirements.txt --quiet

# 4. Configuration template check
if [ ! -f "config.json" ]; then
    echo "Creating config.json from template..."
    cp config.template.json config.json
    echo "IMPORTANT: Remember to edit $TARGET_DIR/config.json with your Unsplash API key and presets."
fi

# 5. Migrate legacy service name if present, then install systemd service
echo "4. Registering systemd service ($SERVICE_NAME)..."
if systemctl list-unit-files | grep -q "^automatic-thumbnail\.service"; then
    echo "Disabling legacy automatic-thumbnail.service..."
    systemctl disable --now automatic-thumbnail.service 2>/dev/null || true
fi
cp "$SERVICE_NAME" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

# 6. Non-destructively link to standard LXC 'update' command
echo "5. Linking to LXC 'update' command..."
UPDATE_BIN="/usr/local/bin/update"
HOOK_SNIPPET="
# === Thumbnail Companion Bridge Update Hook ===
if [ -f /opt/thumbnail-companion-bridge/update.sh ]; then
    bash /opt/thumbnail-companion-bridge/update.sh
fi
"

if [ -f "$UPDATE_BIN" ]; then
    if ! grep -q "thumbnail-companion-bridge" "$UPDATE_BIN"; then
        echo "Appending update hook to existing $UPDATE_BIN..."
        echo "$HOOK_SNIPPET" >> "$UPDATE_BIN"
    else
        echo "Update hook already present in $UPDATE_BIN."
    fi
else
    echo "Creating standard $UPDATE_BIN with update hook..."
    cat <<'EOF' > "$UPDATE_BIN"
#!/usr/bin/env bash
set -e
echo "=== System Update (APT) ==="
apt-get update && apt-get upgrade -y

# === Thumbnail Companion Bridge Update Hook ===
if [ -f /opt/thumbnail-companion-bridge/update.sh ]; then
    bash /opt/thumbnail-companion-bridge/update.sh
fi
EOF
    chmod +x "$UPDATE_BIN"
fi

echo "=========================================================="
echo " Installation Complete!"
echo " Service Status:"
systemctl --no-pager status "$SERVICE_NAME"
echo ""
echo " You can update the service at any time by running:"
echo "   update"
echo " or:"
echo "   /opt/thumbnail-companion-bridge/update.sh"
echo "=========================================================="
