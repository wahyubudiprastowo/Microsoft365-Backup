#!/bin/sh
# ============================================================================
# Microsoft 365 Backup — Local Installer
# Usage: sh install.sh
# ============================================================================

set -e

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BACKUP_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
APP_DIR="$SCRIPT_DIR"

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

APP_UID="${SPO_UID:-1000}"
APP_GID="${SPO_GID:-1000}"

compose_cmd() {
    if command -v docker-compose >/dev/null 2>&1; then
        ${SUDO:+$SUDO }docker-compose "$@"
    else
        ${SUDO:+$SUDO }docker compose "$@"
    fi
}

echo "═══════════════════════════════════════════════════════════"
echo "  Microsoft 365 Backup — Installation"
echo "  Target: $BACKUP_ROOT"
echo "═══════════════════════════════════════════════════════════"

echo "▶ Creating directories..."
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/data"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/data/.manifests"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/config"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/logs"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/redis"

echo "▶ Copying config (if not exists)..."
if [ ! -f "$BACKUP_ROOT/config/config.json" ]; then
    if [ -f "$SCRIPT_DIR/config.example.json" ]; then
        SOURCE_CONFIG="$SCRIPT_DIR/config.example.json"
    else
        SOURCE_CONFIG="$SCRIPT_DIR/config.json"
    fi
    ${SUDO:+$SUDO }cp "$SOURCE_CONFIG" "$BACKUP_ROOT/config/config.json"
    ${SUDO:+$SUDO }chmod 600 "$BACKUP_ROOT/config/config.json"
    echo "  ✓ Config copied"
else
    echo "  ⚠ Config already exists, skipping..."
fi

echo "▶ Setting permissions..."
${SUDO:+$SUDO }chown -R "$APP_UID:$APP_GID" "$BACKUP_ROOT/data" "$BACKUP_ROOT/logs" "$BACKUP_ROOT/redis" "$BACKUP_ROOT/config"
${SUDO:+$SUDO }chmod -R 755 "$BACKUP_ROOT/data" "$BACKUP_ROOT/logs" "$BACKUP_ROOT/redis"
${SUDO:+$SUDO }chmod 750 "$BACKUP_ROOT/config"
${SUDO:+$SUDO }chmod 640 "$BACKUP_ROOT/config/config.json"

echo "▶ Disk space:"
df -h "$BACKUP_ROOT"

echo ""
echo "▶ Building Docker image..."
cd "$APP_DIR"
compose_cmd build

echo "▶ Starting services..."
compose_cmd up -d

sleep 10

echo "▶ Status:"
compose_cmd ps

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✅ Installation Complete!"
echo "═══════════════════════════════════════════════════════════"
echo "  📱 Web UI    : http://$(hostname -I | awk '{print $1}'):5050"
echo "  📂 Backups   : $BACKUP_ROOT/data"
echo "  ⚙️  Config    : $BACKUP_ROOT/config/config.json"
echo "  📋 Logs      : $BACKUP_ROOT/logs"
echo "  👤 Runtime   : UID=$APP_UID GID=$APP_GID"
echo ""
echo "  Useful commands:"
echo "    cd $APP_DIR && docker compose logs -f spo-backup"
echo "    cd $APP_DIR && docker compose restart"
echo "    cd $APP_DIR && docker compose down"
echo ""
