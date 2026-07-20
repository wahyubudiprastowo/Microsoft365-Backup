#!/bin/sh
# ============================================================================
# Microsoft 365 Backup — Local Installer (FIXED v2)
# Usage: sh install-fixed.sh
# Run from: <repo>/spo-backup-final/
# ============================================================================

set -e

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BACKUP_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

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
echo "  Source: $SCRIPT_DIR"
echo "  Target: $BACKUP_ROOT"
echo "═══════════════════════════════════════════════════════════"

# 1. Create persistent directories (mounted by docker compose)
echo ""
echo "▶ Step 1/5: Creating persistent directories..."
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/data"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/data/.manifests"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/config"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/logs"
${SUDO:+$SUDO }mkdir -p "$BACKUP_ROOT/redis"
echo "  ✓ Created: data, config, logs, redis"

# 2. Copy config to persistent location
echo ""
echo "▶ Step 2/5: Setting up config..."
if [ ! -f "$BACKUP_ROOT/config/config.json" ]; then
    if [ -f "$SCRIPT_DIR/config.example.json" ]; then
        SOURCE_CONFIG="$SCRIPT_DIR/config.example.json"
    else
        SOURCE_CONFIG="$SCRIPT_DIR/config.json"
    fi
    ${SUDO:+$SUDO }cp "$SOURCE_CONFIG" "$BACKUP_ROOT/config/config.json"
    ${SUDO:+$SUDO }chmod 600 "$BACKUP_ROOT/config/config.json"
    echo "  ✓ Config template copied to $BACKUP_ROOT/config/config.json"
else
    echo "  ⚠ Config already exists, keeping existing"
fi

# 3. Set permissions
echo ""
echo "▶ Step 3/5: Setting permissions..."
${SUDO:+$SUDO }chown -R "$APP_UID:$APP_GID" "$BACKUP_ROOT/data" "$BACKUP_ROOT/logs" "$BACKUP_ROOT/redis" "$BACKUP_ROOT/config"
${SUDO:+$SUDO }chmod -R 755 "$BACKUP_ROOT/data" "$BACKUP_ROOT/logs" "$BACKUP_ROOT/redis"
${SUDO:+$SUDO }chmod 750 "$BACKUP_ROOT/config"
${SUDO:+$SUDO }chmod 640 "$BACKUP_ROOT/config/config.json"
echo "  ✓ Permissions set"

# 4. Disk space check
echo ""
echo "▶ Step 4/5: Disk space check..."
df -h "$BACKUP_ROOT" | tail -1 | awk '{print "  ✓ Volume: " $1 " | Size: " $2 " | Free: " $4 " (" $5 " used)"}'

# 5. Build & start FROM CURRENT DIRECTORY (where Dockerfile & app/ exist)
echo ""
echo "▶ Step 5/5: Building & starting services..."
cd "$SCRIPT_DIR"
compose_cmd build
compose_cmd up -d

# Wait for healthcheck
echo ""
echo "▶ Waiting for services to start..."
sleep 15

echo ""
echo "▶ Service status:"
compose_cmd ps

# Get host IP
HOST_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✅ Installation Complete!"
echo "═══════════════════════════════════════════════════════════"
echo "  📱 Web UI    : http://${HOST_IP}:5050"
echo "  📂 Backups   : $BACKUP_ROOT/data"
echo "  ⚙️  Config    : $BACKUP_ROOT/config/config.json"
echo "  📋 Logs      : $BACKUP_ROOT/logs"
echo "  👤 Runtime   : UID=$APP_UID GID=$APP_GID"
echo ""
echo "  Useful commands (run from $SCRIPT_DIR):"
echo "    docker compose logs -f spo-backup    # View web logs"
echo "    docker compose logs -f celery-worker # View worker logs"
echo "    docker compose restart               # Restart all"
echo "    docker compose down                  # Stop all"
echo "    docker compose ps                    # Check status"
echo ""
echo "  📌 IMPORTANT: Stay in this folder (or specify -f docker-compose.yml)"
echo "     to use docker compose commands."
echo ""
