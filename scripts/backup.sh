#!/bin/bash
# =============================================================================
# Backup de TeaSpeak (version PostgreSQL).
#
# Reemplaza al backup antiguo que copiaba TeaData.sqlite: ahora los datos viven
# en PostgreSQL, asi que se usa pg_dump. Se respalda:
#   - La base principal "teaspeak" (usuarios, canales, permisos, grupos, bans...)
#   - Los ficheros de runtime: files/, certs/, config.yml, protocol_key.txt
#
# La base de logs "teaspeak_logs" NO se respalda por defecto: es transitoria y
# puede pesar decenas de GB. Actívala con BACKUP_INCLUDE_LOGS=1 si la necesitas.
#
# Retencion: se conservan los ultimos RETENTION_DAYS dias de backups.
# Pensado para cron (diario).
# =============================================================================
set -uo pipefail

INSTALL_DIR="${TEASPEAK_DIR:-/opt/teaspeak}"
BACKUP_DIR="${TEASPEAK_BACKUP_DIR:-${INSTALL_DIR}/backups}"
LOG_FILE="${TEASPEAK_BACKUP_LOG:-${BACKUP_DIR}/backup.log}"
RETENTION_DAYS="${TEASPEAK_BACKUP_RETENTION_DAYS:-30}"

MAIN_DB="${TEASPEAK_DB:-teaspeak}"
LOGS_DB="${TEASPEAK_LOGS_DB:-teaspeak_logs}"
INCLUDE_LOGS="${BACKUP_INCLUDE_LOGS:-0}"

DATE=$(date +"%Y%m%d_%H%M%S")

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"; }
fail() { log "ERROR - $1"; exit 1; }

mkdir -p "$BACKUP_DIR" || { echo "no se pudo crear ${BACKUP_DIR}"; exit 1; }
log "=== Iniciando backup ==="

# --- 1. Volcado de la base principal ---
db_dump="${BACKUP_DIR}/teaspeak_db_${DATE}.sql.gz"
if sudo -u postgres pg_dump --no-owner --no-privileges "${MAIN_DB}" 2>>"$LOG_FILE" | gzip > "${db_dump}"; then
    log "Base ${MAIN_DB} volcada: $(basename "${db_dump}") ($(du -h "${db_dump}" | cut -f1))"
else
    fail "fallo el pg_dump de ${MAIN_DB}"
fi

# --- 2. Volcado opcional de la base de logs ---
if [[ "${INCLUDE_LOGS}" == "1" ]]; then
    logs_dump="${BACKUP_DIR}/teaspeak_logs_${DATE}.sql.gz"
    if sudo -u postgres pg_dump --no-owner --no-privileges "${LOGS_DB}" 2>>"$LOG_FILE" | gzip > "${logs_dump}"; then
        log "Base ${LOGS_DB} volcada: $(basename "${logs_dump}") ($(du -h "${logs_dump}" | cut -f1))"
    else
        log "AVISO: fallo el pg_dump de ${LOGS_DB} (se continua)"
    fi
fi

# --- 3. Ficheros de runtime ---
files_backup="${BACKUP_DIR}/teaspeak_files_${DATE}.tar.gz"
to_backup=()
for item in files certs resources config.yml protocol_key.txt query_ip_whitelist.txt; do
    [[ -e "${INSTALL_DIR}/${item}" ]] && to_backup+=("${item}")
done
if [[ "${#to_backup[@]}" -gt 0 ]]; then
    if tar -czf "${files_backup}" -C "${INSTALL_DIR}" "${to_backup[@]}" 2>>"$LOG_FILE"; then
        log "Ficheros respaldados: $(basename "${files_backup}") ($(du -h "${files_backup}" | cut -f1)) [${to_backup[*]}]"
    else
        fail "fallo el tar de los ficheros de runtime"
    fi
else
    log "AVISO: no se encontraron ficheros de runtime que respaldar en ${INSTALL_DIR}"
fi

# --- 4. Retencion ---
find "${BACKUP_DIR}" -name 'teaspeak_db_*.sql.gz'    -type f -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null
find "${BACKUP_DIR}" -name 'teaspeak_logs_*.sql.gz'  -type f -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null
find "${BACKUP_DIR}" -name 'teaspeak_files_*.tar.gz' -type f -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null
log "Limpieza de backups de mas de ${RETENTION_DAYS} dias completada."

# Recorta el propio log para que no crezca sin fin
tail -n 2000 "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null && mv "${LOG_FILE}.tmp" "$LOG_FILE"
log "=== Backup finalizado ==="
exit 0
