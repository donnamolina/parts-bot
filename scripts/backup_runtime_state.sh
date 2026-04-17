#!/usr/bin/env bash
set -euo pipefail
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/opt/parts-bot/backups/${TIMESTAMP}"
mkdir -p "${BACKUP_DIR}"
cp -r /opt/parts-bot/auth "${BACKUP_DIR}/auth" 2>/dev/null || cp -r /opt/parts-bot/auth_info "${BACKUP_DIR}/auth_info" 2>/dev/null || echo "no auth dir"
cp /opt/parts-bot/cache/translation_cache.json "${BACKUP_DIR}/" 2>/dev/null || echo "no translation_cache"
cp /opt/parts-bot/cache/ebay_token.json "${BACKUP_DIR}/" 2>/dev/null || echo "no ebay_token"
cp /opt/parts-bot/cache/vehicles.json "${BACKUP_DIR}/" 2>/dev/null || echo "no vehicles cache"
cp /opt/parts-bot/.env "${BACKUP_DIR}/" 2>/dev/null || echo "no .env"
cd /opt/parts-bot/backups && ls -1t | tail -n +11 | xargs -r rm -rf
echo "Backup written to ${BACKUP_DIR}"
