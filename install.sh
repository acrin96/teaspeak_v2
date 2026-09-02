#!/bin/bash
#
# TeaSpeak v2 installer  (fork basado en 1.4.21-beta-3, PostgreSQL)
# Plataforma soportada: Debian 11 (bullseye). Ejecutar como root.
#
# Variables opcionales (env):
#   INSTALL_DIR   directorio de instalacion        (def: /opt/teaspeak)
#   RUN_USER      usuario de sistema del servicio   (def: teaspeak)
#   DB_USER       rol PostgreSQL                    (def: teaspeak)
#   DB_NAME       base de datos principal           (def: teaspeak)
#   LOGS_DB       base de datos de logs             (def: teaspeak_logs)
#   SERVICE       nombre del servicio systemd       (def: teaspeak)
#   LOGS_CAP_GIB  tope FIFO de la base de logs (GiB) (def: 25)
#   PROTOCOL_KEY_B64  contenido de protocol_key.txt en base64 (opcional)
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/teaspeak}"
RUN_USER="${RUN_USER:-teaspeak}"
DB_USER="${DB_USER:-teaspeak}"
DB_NAME="${DB_NAME:-teaspeak}"
LOGS_DB="${LOGS_DB:-teaspeak_logs}"
SERVICE="${SERVICE:-teaspeak}"
LOGS_CAP_GIB="${LOGS_CAP_GIB:-25}"

RELEASE_TARBALL_URL="${RELEASE_TARBALL_URL:-https://github.com/acrin96/teaspeak_v2/releases/latest/download/teaspeak_v2_1.4.21-beta-3_linux_amd64.tar.gz}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say(){ echo -e "\e[1;32m[install]\e[0m $*"; }
die(){ echo -e "\e[1;31m[install] ERROR:\e[0m $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "ejecuta como root."

if [ -r /etc/os-release ]; then . /etc/os-release; fi
if [ "${ID:-}" != "debian" ] || [ "${VERSION_ID:-}" != "11" ]; then
    echo "AVISO: plataforma soportada = Debian 11. Detectado: ${PRETTY_NAME:-desconocido}. Continuo bajo tu responsabilidad."
fi

# --- bootstrap: si el binario no esta junto al script, descargar el paquete de la release ---
if [ ! -f "$SRC/TeaSpeakServer" ]; then
    say "No encuentro el binario junto al script; descargando el paquete de la release..."
    export DEBIAN_FRONTEND=noninteractive
    command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }
    command -v tar  >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq tar; }
    BOOT_TMP="$(mktemp -d)"
    curl -fsSL "$RELEASE_TARBALL_URL" -o "$BOOT_TMP/bundle.tar.gz" || die "no pude descargar el paquete desde $RELEASE_TARBALL_URL"
    tar -xzf "$BOOT_TMP/bundle.tar.gz" -C "$BOOT_TMP" || die "no pude extraer el paquete descargado."
    SRC="$BOOT_TMP/teaspeak_v2_bundle"
    [ -f "$SRC/TeaSpeakServer" ] || die "el paquete descargado no contiene TeaSpeakServer."
    say "Paquete descargado y extraido en $SRC"
fi

say "Instalando dependencias del sistema (apt)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    postgresql postgresql-contrib libpq5 libsqlite3-0 \
    libnice10 libgupnp-1.2-0 libgupnp-igd-1.0-4 libgssdp-1.2-0 libsoup2.4-1 \
    libglib2.0-0 libgnutls30 libxml2 libpcre3 libbrotli1 libpsl5 libicu67 \
    ca-certificates coreutils >/dev/null

say "Arrancando PostgreSQL..."
systemctl enable --now postgresql >/dev/null 2>&1 || die "no pude arrancar postgresql."

# --- usuario de sistema ---
if ! id "$RUN_USER" >/dev/null 2>&1; then
    say "Creando usuario de sistema '$RUN_USER'..."
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$RUN_USER"
fi

# --- ¿upgrade o instalacion nueva? ---
UPGRADE=0
[ -f "$INSTALL_DIR/config.yml" ] && UPGRADE=1

# --- credenciales de BD ---
if [ "$UPGRADE" = 0 ]; then
    DB_PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
    say "Configurando rol PostgreSQL '$DB_USER'..."
    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        sudo -u postgres psql -c "ALTER ROLE \"$DB_USER\" LOGIN PASSWORD '$DB_PASS';" >/dev/null
    else
        sudo -u postgres psql -c "CREATE ROLE \"$DB_USER\" LOGIN PASSWORD '$DB_PASS';" >/dev/null
    fi
else
    say "config.yml existente detectado: modo upgrade (se conserva config y credenciales)."
fi

# --- bases de datos ---
for db in "$DB_NAME" "$LOGS_DB"; do
    if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
        say "Creando base de datos '$db'..."
        sudo -u postgres psql -c "CREATE DATABASE \"$db\" OWNER \"$DB_USER\";" >/dev/null
    fi
done

# --- directorios y ficheros ---
say "Desplegando en $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"/{libs,resources,providers,certs,files,logs,crash_dumps}
install -m 0755 "$SRC/TeaSpeakServer" "$INSTALL_DIR/TeaSpeakServer"
cp -a "$SRC/libs/." "$INSTALL_DIR/libs/"
cp -a "$SRC/resources/." "$INSTALL_DIR/resources/"
install -m 0755 "$SRC/scripts/logs_retention.sh" "$INSTALL_DIR/logs_retention.sh"

# --- config ---
if [ "$UPGRADE" = 0 ]; then
    say "Generando config.yml ..."
    sed -e "s#__DB_USER__#$DB_USER#g" -e "s#__DB_PASS__#$DB_PASS#g" \
        -e "s#__DB_NAME__#$DB_NAME#g" -e "s#__LOGS_DB__#$LOGS_DB#g" \
        "$SRC/config.template.yml" > "$INSTALL_DIR/config.yml"
    chmod 640 "$INSTALL_DIR/config.yml"
fi

# --- protocol_key ---
if [ -n "${PROTOCOL_KEY_B64:-}" ]; then
    say "Escribiendo protocol_key.txt desde PROTOCOL_KEY_B64 ..."
    echo "$PROTOCOL_KEY_B64" | base64 -d > "$INSTALL_DIR/protocol_key.txt"
    chmod 600 "$INSTALL_DIR/protocol_key.txt"
elif [ ! -f "$INSTALL_DIR/protocol_key.txt" ]; then
    echo -e "\e[1;33m[install] AVISO:\e[0m no hay protocol_key.txt."
    echo "  Sube tu clave a $INSTALL_DIR/protocol_key.txt (chmod 600) o reinstala con PROTOCOL_KEY_B64 definido."
    echo "  Sin ella el servidor generara una identidad nueva (cambia la clave publica del servidor)."
fi

chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

# --- servicio systemd ---
say "Instalando servicio systemd '$SERVICE'..."
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=TeaSpeak Server (v2, PostgreSQL)
After=network-online.target postgresql.service
Wants=network-online.target
Requires=postgresql.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=LD_LIBRARY_PATH=$INSTALL_DIR/libs
ExecStart=$INSTALL_DIR/TeaSpeakServer
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1

# --- cron de retencion (tope FIFO de la base de logs) ---
say "Instalando cron de retencion de logs (tope ${LOGS_CAP_GIB} GiB)..."
CRON_LINE="0 * * * * $INSTALL_DIR/logs_retention.sh $LOGS_DB $LOGS_CAP_GIB >> /var/log/teaspeak_logs_retention.log 2>&1"
( crontab -l 2>/dev/null | grep -v "$INSTALL_DIR/logs_retention.sh"; echo "$CRON_LINE" ) | crontab -

# --- arranque ---
say "Arrancando el servidor..."
systemctl restart "$SERVICE"
sleep 8
if systemctl is-active --quiet "$SERVICE"; then
    say "TeaSpeak activo. Estado: systemctl status $SERVICE"
    [ "$UPGRADE" = 0 ] && say "Revisa el log para la contrasena inicial de 'serveradmin' y la clave de privilegio: journalctl -u $SERVICE | grep -iE 'serveradmin|token'"
else
    die "el servicio no arranco. Revisa: journalctl -u $SERVICE -n 60 --no-pager"
fi
