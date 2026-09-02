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
# Directorio del script. Con 'curl | bash' no hay fichero (BASH_SOURCE vacio): usamos el CWD.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
[ -n "$SRC" ] || SRC="$PWD"

say(){ echo -e "\e[1;32m[install]\e[0m $*"; }
die(){ echo -e "\e[1;31m[install] ERROR:\e[0m $*" >&2; exit 1; }
# psql como postgres desde /tmp (evita el aviso 'could not change directory' cuando el CWD
# es un directorio al que el usuario postgres no puede entrar, p.ej. /root/...)
PSQL(){ ( cd /tmp && sudo -u postgres psql "$@" ); }

[ "$(id -u)" = 0 ] || die "ejecuta como root."

# silenciar el aviso 'sudo: unable to resolve host <hostname>' anadiendo el hostname a /etc/hosts
HN="$(hostname 2>/dev/null || true)"
if [ -n "$HN" ] && ! grep -q "[[:space:]]$HN\$" /etc/hosts 2>/dev/null && ! grep -q "[[:space:]]$HN[[:space:]]" /etc/hosts 2>/dev/null; then
    echo "127.0.1.1 $HN" >> /etc/hosts 2>/dev/null || true
fi

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
    ca-certificates coreutils cron >/dev/null
systemctl enable --now cron >/dev/null 2>&1 || true

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
    if PSQL -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        PSQL -c "ALTER ROLE \"$DB_USER\" LOGIN PASSWORD '$DB_PASS';" >/dev/null
    else
        PSQL -c "CREATE ROLE \"$DB_USER\" LOGIN PASSWORD '$DB_PASS';" >/dev/null
    fi
else
    say "config.yml existente detectado: modo upgrade (se conserva config y credenciales)."
fi

# --- bases de datos ---
for db in "$DB_NAME" "$LOGS_DB"; do
    if ! PSQL -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
        say "Creando base de datos '$db'..."
        PSQL -c "CREATE DATABASE \"$db\" OWNER \"$DB_USER\";" >/dev/null
    fi
done

# --- directorios y ficheros ---
say "Desplegando en $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"/{libs,resources,providers,certs,files,logs,crash_dumps,scripts}
install -m 0755 "$SRC/TeaSpeakServer" "$INSTALL_DIR/TeaSpeakServer"
cp -a "$SRC/libs/." "$INSTALL_DIR/libs/"
cp -a "$SRC/resources/." "$INSTALL_DIR/resources/"

# --- scripts auxiliares (firewall, backup, retencion de logs) ---
# se toman del paquete si estan, o se descargan del repo publico para tener la version actual
SCRIPTS_BASE="${SCRIPTS_BASE:-https://raw.githubusercontent.com/acrin96/teaspeak_v2/main/scripts}"
for s in firewall.sh backup.sh logs_retention.sh; do
    if [ -f "$SRC/scripts/$s" ]; then
        install -m 0755 "$SRC/scripts/$s" "$INSTALL_DIR/scripts/$s"
    else
        curl -fsSL "$SCRIPTS_BASE/$s" -o "$INSTALL_DIR/scripts/$s" 2>/dev/null && chmod +x "$INSTALL_DIR/scripts/$s" || \
            echo -e "\e[1;33m[install] AVISO:\e[0m no pude obtener $s (podras añadirlo luego)."
    fi
done

# --- config ---
if [ "$UPGRADE" = 0 ]; then
    say "Generando config.yml ..."
    sed -e "s#__DB_USER__#$DB_USER#g" -e "s#__DB_PASS__#$DB_PASS#g" \
        -e "s#__DB_NAME__#$DB_NAME#g" -e "s#__LOGS_DB__#$LOGS_DB#g" \
        "$SRC/config.template.yml" > "$INSTALL_DIR/config.yml"
    chmod 640 "$INSTALL_DIR/config.yml"
fi

# --- protocol_key ---
# Precedencia: PROTOCOL_KEY_B64 (base64) -> protocol_key.txt hallado (sin formato, p.ej. en /root)
#              -> el ya instalado (upgrade) -> aviso (identidad nueva).
PK_DST="$INSTALL_DIR/protocol_key.txt"
if [ -n "${PROTOCOL_KEY_B64:-}" ]; then
    say "Escribiendo protocol_key.txt desde PROTOCOL_KEY_B64 ..."
    echo "$PROTOCOL_KEY_B64" | base64 -d > "$PK_DST"
    chmod 600 "$PK_DST"
else
    PK_SRC=""
    for cand in "$PWD/protocol_key.txt" "/root/protocol_key.txt" "$SRC/protocol_key.txt"; do
        if [ -f "$cand" ] && [ "$(readlink -f "$cand" 2>/dev/null)" != "$(readlink -f "$PK_DST" 2>/dev/null)" ]; then
            PK_SRC="$cand"; break
        fi
    done
    if [ -n "$PK_SRC" ]; then
        say "Usando protocol_key.txt encontrado en $PK_SRC"
        install -m 600 "$PK_SRC" "$PK_DST"
    elif [ ! -f "$PK_DST" ]; then
        echo -e "\e[1;33m[install] AVISO:\e[0m no hay protocol_key.txt."
        echo "  Deja tu clave (sin formato) en /root/protocol_key.txt ANTES de instalar, o define PROTOCOL_KEY_B64."
        echo "  Sin ella el servidor generara una identidad nueva (cambia la clave publica del servidor)."
    fi
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

# --- crons: retencion de logs (horario) + backup (diario) ---
say "Instalando crons (retencion de logs ${LOGS_CAP_GIB} GiB + backup diario)..."
RET_LINE="0 * * * * $INSTALL_DIR/scripts/logs_retention.sh $LOGS_DB $LOGS_CAP_GIB >> /var/log/teaspeak_logs_retention.log 2>&1"
BK_LINE="30 4 * * * TEASPEAK_DIR=$INSTALL_DIR TEASPEAK_DB=$DB_NAME TEASPEAK_LOGS_DB=$LOGS_DB $INSTALL_DIR/scripts/backup.sh >> /var/log/teaspeak_backup.log 2>&1"
( ( crontab -l 2>/dev/null || true ) | grep -vE "$INSTALL_DIR/scripts/(logs_retention|backup)\.sh"; echo "$RET_LINE"; echo "$BK_LINE" ) | crontab - || \
    echo -e "\e[1;33m[install] AVISO:\e[0m no pude instalar los crons; añadelos a mano."

# --- arranque ---
say "Arrancando el servidor..."
systemctl restart "$SERVICE"
sleep 8
if ! systemctl is-active --quiet "$SERVICE"; then
    die "el servicio no arranco. Revisa: journalctl -u $SERVICE -n 60 --no-pager"
fi
say "TeaSpeak activo."

# --- reinicio de asentamiento (solo instalacion nueva) ---
# En el primer arranque con BD nueva, la identidad del servidor (protocol_key) y los datos de
# instancia recien creados terminan de aplicarse tras un reinicio. Lo hacemos aqui para que la
# clave de protocolo quede activa de fabrica sin que el usuario reinicie a mano.
if [ "$UPGRADE" = 0 ]; then
    say "Reinicio de asentamiento (aplica la identidad/protocol_key)..."
    systemctl restart "$SERVICE"
    sleep 6
    systemctl is-active --quiet "$SERVICE" || die "el servicio no quedo activo tras el reinicio de asentamiento."
fi

# --- credenciales iniciales (solo instalacion nueva) ---
if [ "$UPGRADE" = 0 ]; then
    QPW=""; TKN=""
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        QPW=$(PSQL -d "$DB_NAME" -tAc "SELECT password FROM queries WHERE username='serveradmin' ORDER BY server LIMIT 1;" 2>/dev/null | tr -d '[:space:]')
        TKN=$(PSQL -d "$DB_NAME" -tAc "SELECT token FROM tokens ORDER BY created DESC LIMIT 1;" 2>/dev/null | tr -d '[:space:]')
        [ -n "$QPW" ] && [ -n "$TKN" ] && break
        sleep 2
    done
    echo ""
    echo -e "\e[1;32m===================== CREDENCIALES =====================\e[0m"
    echo "  ServerQuery (YaTQA/bots):"
    echo "     usuario:   serveradmin"
    echo "     password:  ${QPW:-<vacio: revisa 'SELECT * FROM queries;'>}"
    echo "     puerto:    10101 (TCP)"
    echo ""
    echo "  Clave de privilegio (grupo Server Admin):"
    echo "     ${TKN:-<vacio: revisa 'SELECT * FROM tokens;'>}"
    echo ""
    echo "  Base de datos (en $INSTALL_DIR/config.yml -> general.database.url):"
    echo "     usuario: $DB_USER   base: $DB_NAME   (contrasena embebida en la url)"
    echo -e "\e[1;32m========================================================\e[0m"
    echo ""
    echo "  Scripts en $INSTALL_DIR/scripts/ :"
    echo "     backup.sh        -> backup diario ya programado (cron 04:30)"
    echo "     logs_retention.sh-> tope de logs ya programado (cron horario)"
    echo "     firewall.sh      -> NO se ejecuta solo (riesgo de bloquear SSH)."
    echo "                         Revisa las IPs de whitelist y lanzalo tu:"
    echo "                         sudo WHITELIST_SSH=\"TU_IP\" WHITELIST_QUERY=\"TU_IP\" WHITELIST_DB=\"TU_IP\" bash $INSTALL_DIR/scripts/firewall.sh"
    echo "  Nota: la mitigacion anti-crash (validacion DER del handshake) va compilada en el"
    echo "        binario; no hay script 'anticrash' aparte."
fi
