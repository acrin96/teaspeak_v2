#!/bin/bash
#
# TeaSpeak InstanceLogs — tope FIFO de tamano para la 2a base PostgreSQL.
#
# Borra las filas mas antiguas (por `timestamp`, ms epoch) de todas las tablas
# logs_* cuando el tamano en disco de la base supera el tope, hasta bajar al
# objetivo. Mide con pg_database_size (tamano real en disco) y hace VACUUM FULL
# de las tablas podadas dentro del bucle para devolver el espacio al SO; asi el
# tope de 25 GB es un limite de disco efectivo, no solo de datos vivos.
# VACUUM FULL bloquea cada tabla brevemente, pero como el filtro de acciones de
# query mantiene el volumen bajo, este backstop rara vez se dispara.
# Pensado para ejecutarse por cron (p.ej. cada hora).
#
# Uso:  logs_retention.sh [DB] [CAP_GIB]
#   DB      nombre de la base de logs   (default: teaspeak14_logs)
#   CAP_GIB tope en GiB                 (default: 25)
#
set -u

DB="${1:-teaspeak14_logs}"
CAP_GIB="${2:-25}"
CAP_BYTES=$(( CAP_GIB * 1024 * 1024 * 1024 ))
# override en bytes (solo para pruebas): TEASPEAK_LOGS_CAP_BYTES
if [ -n "${TEASPEAK_LOGS_CAP_BYTES:-}" ]; then CAP_BYTES="$TEASPEAK_LOGS_CAP_BYTES"; fi
TARGET_BYTES=$(( CAP_BYTES * 90 / 100 ))   # tras podar, dejar en el 90% del tope
BATCH_FRACTION=10                          # borra ~1/10 del rango temporal por iteracion
MAX_ITER=50

TABLES="logs_channel logs_client_channel logs_client_edit logs_custom logs_files logs_group_assignments logs_groups logs_permission logs_query_authenticate logs_query_server logs_server logs_server_edit"

# psql como el propietario del cluster (ajustar si se usa otro rol/host)
PSQL() { sudo -u postgres psql -d "$DB" -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

# tamano real en disco de la base de logs
db_size() { PSQL "SELECT pg_database_size('$DB');"; }

now_size=$(db_size)
if [ "${now_size:-0}" -le "$CAP_BYTES" ]; then
    echo "$(date -Is) OK: ${now_size} bytes <= tope ${CAP_BYTES} (${CAP_GIB} GiB); nada que podar."
    exit 0
fi

echo "$(date -Is) TOPE EXCEDIDO: ${now_size} bytes > ${CAP_BYTES}; podando por FIFO..."

iter=0
while [ "${now_size:-0}" -gt "$TARGET_BYTES" ] && [ "$iter" -lt "$MAX_ITER" ]; do
    iter=$(( iter + 1 ))

    # rango temporal global (min/max timestamp entre todas las tablas)
    minmax=$(PSQL "$(printf 'SELECT MIN(t), MAX(t) FROM (%s) x;' \
        "$(for t in $TABLES; do printf 'SELECT MIN(timestamp) AS t FROM %s UNION ALL SELECT MAX(timestamp) FROM %s UNION ALL ' "$t" "$t"; done | sed 's/UNION ALL $//')")")
    # minmax viene como "min|max"
    tmin="${minmax%%|*}"; tmax="${minmax##*|}"
    if [ -z "$tmin" ] || [ -z "$tmax" ] || [ "$tmin" = "$tmax" ]; then
        echo "  sin rango temporal util; abortando poda."
        break
    fi

    # cortar todo lo anterior a min + (rango/BATCH_FRACTION)
    cutoff=$(( tmin + (tmax - tmin) / BATCH_FRACTION ))
    [ "$cutoff" -le "$tmin" ] && cutoff=$(( tmin + 1 ))

    for t in $TABLES; do
        PSQL "DELETE FROM $t WHERE timestamp < $cutoff;" >/dev/null
        # VACUUM FULL devuelve el espacio al SO para que pg_database_size baje
        PSQL "VACUUM FULL $t;" >/dev/null
    done

    now_size=$(db_size)
    echo "  iter $iter: cutoff=$cutoff -> ${now_size} bytes"
done

echo "$(date -Is) FIN: ${now_size} bytes (objetivo ${TARGET_BYTES})."
