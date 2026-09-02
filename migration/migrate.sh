#!/bin/bash
#
# Migra un backup de TeaSpeak (version anterior, SQLite) a esta instalacion PostgreSQL.
# Uso:  sudo bash migrate.sh /ruta/al/teaspeak_backup_XXXXXX.tar.gz
#
# El backup debe contener: TeaData.sqlite y el arbol files/ (iconos, avatares, conversaciones).
# NO toca el config.yml de la instalacion nueva (se conserva PostgreSQL + claves nuevas).
#
set -uo pipefail
BACKUP="${1:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/teaspeak}"
SERVICE="${SERVICE:-teaspeak}"
DB_NAME="${DB_NAME:-teaspeak}"
MIG_PY="${MIG_PY:-/root/migrate.py}"

say(){ echo -e "\e[1;32m[migrate]\e[0m $*"; }
die(){ echo -e "\e[1;31m[migrate] ERROR:\e[0m $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "ejecuta como root."
[ -n "$BACKUP" ] && [ -f "$BACKUP" ] || die "pasa la ruta del backup: sudo bash migrate.sh /root/teaspeak_backup_XXXX.tar.gz"
# el motor de migracion (migrate.py): usar el local si esta, o descargarlo del repo
if [ ! -f "$MIG_PY" ]; then
    MIG_PY=/root/migrate.py
    if [ ! -f "$MIG_PY" ]; then
        say "Descargando migrate.py del repositorio..."
        curl -fsSL "https://raw.githubusercontent.com/acrin96/teaspeak_v2/main/migration/migrate.py" -o "$MIG_PY" \
            || die "no pude descargar migrate.py; colocalo en /root/migrate.py"
    fi
fi

export DEBIAN_FRONTEND=noninteractive
say "Instalando dependencias (sqlite3, python3-psycopg2)..."
apt-get install -y -qq sqlite3 python3 python3-psycopg2 >/dev/null 2>&1 || die "no pude instalar dependencias."

WORK="$(mktemp -d)"
say "Extrayendo backup en $WORK ..."
tar -xzf "$BACKUP" -C "$WORK" || die "no pude extraer el backup."
[ -f "$WORK/TeaData.sqlite" ] || die "el backup no contiene TeaData.sqlite."
[ -d "$WORK/files" ] || say "AVISO: el backup no trae carpeta files/ (no habra iconos/avatares que copiar)."

# sqlite en una ruta legible por el usuario postgres (/root no lo es)
SQ=/tmp/teaspeak_migrate.sqlite
cp "$WORK/TeaData.sqlite" "$SQ"; chmod 644 "$SQ"

say "Parando el servidor..."
systemctl stop "$SERVICE" 2>/dev/null; sleep 1

# SQLite no impone longitudes ni tamaño de entero; los datos de produccion pueden exceder
# los VARCHAR(n)/INTEGER del esquema PostgreSQL. Ensanchamos a TEXT/BIGINT (idempotente) para
# que la copia no falle por "value too long" ni "integer out of range".
say "Ajustando tipos del esquema (VARCHAR->TEXT, INTEGER no-identity->BIGINT)..."
WIDEN=$(sudo -u postgres psql -d "$DB_NAME" -tAc "
SELECT 'ALTER TABLE \"'||table_name||'\" ALTER COLUMN \"'||column_name||'\" TYPE '||
       CASE WHEN data_type='character varying' THEN 'TEXT' ELSE 'BIGINT' END||';'
FROM information_schema.columns
WHERE table_schema='public'
  AND ( data_type='character varying'
        OR (data_type='integer' AND is_identity='NO') );" 2>/dev/null)
[ -n "$WIDEN" ] && echo "$WIDEN" | sudo -u postgres psql -d "$DB_NAME" >/dev/null 2>&1

say "Migrando la base de datos a PostgreSQL ($DB_NAME)..."
cp "$MIG_PY" /tmp/migrate.py; chmod 644 /tmp/migrate.py
sudo -u postgres python3 /tmp/migrate.py "$SQ" "$DB_NAME" || die "fallo la migracion de la base de datos (no se commiteo)."

if [ -d "$WORK/files" ]; then
    say "Copiando ficheros (iconos, avatares, conversaciones)..."
    mkdir -p "$INSTALL_DIR/files"
    rm -rf "$INSTALL_DIR/files"/* 2>/dev/null
    cp -a "$WORK/files/." "$INSTALL_DIR/files/"
fi

# geoloc del backup, por si difiere (opcional; no falla si no esta)
if [ -d "$WORK/geoloc" ]; then
    say "Actualizando geoloc desde el backup..."
    mkdir -p "$INSTALL_DIR/geoloc"; cp -a "$WORK/geoloc/." "$INSTALL_DIR/geoloc/"
fi

RUN_USER="$(stat -c '%U' "$INSTALL_DIR/TeaSpeakServer" 2>/dev/null || echo teaspeak)"
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR/files" "$INSTALL_DIR/geoloc" 2>/dev/null || true

say "Arrancando el servidor..."
systemctl start "$SERVICE"; sleep 8
systemctl is-active --quiet "$SERVICE" || die "el servidor no arranco tras migrar. Revisa: journalctl -u $SERVICE -n 60 --no-pager"

echo
say "=== Verificacion ==="
cd /tmp
PGT(){ sudo -u postgres psql -d "$DB_NAME" -tAc "$1" 2>/dev/null | tr -d ' '; }
echo "  servidores virtuales : $(PGT 'SELECT count(*) FROM servers;')  (IDs: $(PGT 'SELECT string_agg(serverid::text,\",\" ORDER BY serverid) FROM servers;'))"
echo "  canales              : $(PGT 'SELECT count(*) FROM channels;')"
echo "  grupos               : $(PGT 'SELECT count(*) FROM groups;')"
echo "  clientes (global)    : $(PGT 'SELECT count(*) FROM clients;')"
echo "  permisos             : $(PGT 'SELECT count(*) FROM permissions;')"
echo "  propiedades          : $(PGT 'SELECT count(*) FROM properties;')"
echo "  bans                 : $(PGT 'SELECT count(*) FROM bannedclients;')"
echo "  cuentas query        : $(PGT 'SELECT count(*) FROM queries;')"
ICONS=$(find "$INSTALL_DIR/files" -type f -name 'icon_*' 2>/dev/null | wc -l)
echo "  ficheros de icono    : $ICONS"
echo "  grupos con icono !=0 : $(PGT "SELECT count(*) FROM properties WHERE key='iconid' AND value<>'0' AND value<>'';")"
echo "  unit                 : $(systemctl is-active $SERVICE)"
rm -rf "$WORK" "$SQ"
say "Migracion completada."
