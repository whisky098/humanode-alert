#!/usr/bin/env bash
# Installs humanode-alert as a systemd service: sudo ./install.sh
# Run it again to update, or remove it with: sudo ./install.sh uninstall
set -euo pipefail

APP_DIR=/opt/humanode-alert
CONFIG=/etc/humanode-alert/config.env
SERVICE=/etc/systemd/system/humanode-alert.service
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0"; exit 1; }

if [ "${1:-}" = "uninstall" ]; then
    systemctl disable --now humanode-alert 2>/dev/null || true
    rm -rf "$APP_DIR" "$SERVICE" /var/lib/humanode-alert
    systemctl daemon-reload
    echo "Removed. The config is still in $CONFIG, delete it if you no longer need it."
    exit 0
fi

PYTHON="$(command -v python3)" || { echo "python3 is required: apt install python3"; exit 1; }

set_value() {
    local escaped
    escaped="$(printf '%s' "$2" | sed 's/[&|\\]/\\&/g')"
    sed -i "s|^$1=.*|$1=$escaped|" "$CONFIG"
}

if [ ! -f "$CONFIG" ]; then
    echo "Creating $CONFIG"
    read -rp "Telegram bot token (from @BotFather): " token
    read -rp "Linux user that runs the Humanode node [root]: " node_user
    node_user="${node_user:-root}"
    node_home="$(getent passwd "$node_user" | cut -d: -f6)" || { echo "Unknown user $node_user"; exit 1; }
    read -rp "Humanode workspace [$node_home/.humanode/workspaces/default]: " workspace
    default_rpc="$("$PYTHON" "$SRC_DIR/humanode_alert.py" --detect-rpc || true)"
    if [ -n "$default_rpc" ]; then
        echo "Humanode RPC found at $default_rpc"
    else
        default_rpc=http://127.0.0.1:9933
        echo "Humanode RPC does not respond on 127.0.0.1:9944 or 127.0.0.1:9933, is the node running?"
    fi
    read -rp "Node RPC URL [$default_rpc]: " rpc_url

    install -d -m 755 "$(dirname "$CONFIG")"
    install -m 600 "$SRC_DIR/config.env.example" "$CONFIG"
    set_value TELEGRAM_TOKEN "$token"
    set_value HUMANODE_WORKSPACE "${workspace:-$node_home/.humanode/workspaces/default}"
    set_value RPC_URL "${rpc_url:-$default_rpc}"
    echo "# Linux user the service runs as (same user as the node)" >> "$CONFIG"
    echo "SERVICE_USER=$node_user" >> "$CONFIG"
fi

register_code="$(sed -n 's/^REGISTER_CODE=//p' "$CONFIG")"
if [ -z "$register_code" ]; then
    register_code="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(8))')"
    if grep -q '^REGISTER_CODE=' "$CONFIG"; then
        set_value REGISTER_CODE "$register_code"
    else  # config from a version without the code
        echo "# Code to send with /register, so that only you can claim the bot" >> "$CONFIG"
        echo "REGISTER_CODE=$register_code" >> "$CONFIG"
    fi
fi

service_user="$(sed -n 's/^SERVICE_USER=//p' "$CONFIG")"

install -d "$APP_DIR"
install -m 755 "$SRC_DIR/humanode_alert.py" "$APP_DIR/humanode_alert.py"

cat > "$SERVICE" <<EOF
[Unit]
Description=Humanode BioAuth Telegram alert
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$CONFIG
ExecStart=$PYTHON $APP_DIR/humanode_alert.py
User=${service_user:-root}
StateDirectory=humanode-alert
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable humanode-alert
systemctl restart humanode-alert

echo
echo "Installed. Open your bot in Telegram and send:"
echo
echo "    /register $register_code"
echo
echo "Then send /help for all commands."
echo "Settings: $CONFIG (then: systemctl restart humanode-alert)"
echo "Logs:     journalctl -u humanode-alert -f"
