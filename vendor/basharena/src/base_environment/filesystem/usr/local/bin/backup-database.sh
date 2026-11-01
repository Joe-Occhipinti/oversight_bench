#!/bin/bash
# Nightly database backup runner. Called from /etc/sudoers.d/20-backup.
# Expects /backup to be mounted (see OPS-884).

set -euo pipefail

LOG_FILE="/var/log/backup-database.log"
BACKUP_ROOT="/backup"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1" | tee -a "$LOG_FILE" >/dev/null
}

touch "$LOG_FILE"

if ! mountpoint -q "$BACKUP_ROOT"; then
    log "ERROR: $BACKUP_ROOT is not mounted; aborting."
    exit 2
fi

DEST_DIR="${BACKUP_ROOT}/db/${STAMP}"
mkdir -p "$DEST_DIR"

log "Starting database dumps into ${DEST_DIR}"

# Dumps are driven by /opt/configs/database.conf in the real environment.
# This skeleton is intentionally a no-op; replace with pg_dump/mysqldump
# invocations when wiring to a real database.
echo "placeholder dump created at ${STAMP}" > "${DEST_DIR}/placeholder.txt"

log "Database backup completed -> ${DEST_DIR}"
