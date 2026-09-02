#!/bin/bash
# =============================================================================
# Instalador de un comando - TeaSpeak parcheado + PostgreSQL, Debian 11
#
# Monta en una VPS Debian 11 limpia:
#   - PostgreSQL local + usuario + dos bases (teaspeak, teaspeak_logs)
#   - El binario parcheado + librerias + resources + config + protocol_key
#     (desde ./bundle/, ver make_bundle.sh)
#   - Servicio systemd con reinicio automatico (anticrash)
#   - Cron de retencion de logs (FIFO por tamano) y de backup (pg_dump)
#   - Firewall (firewall.sh)
#
# Uso:   sudo ./install.sh
# =============================================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[OK]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "\n${CYAN}>>>${NC} ${BOLD}$1${NC}"; }
info() { echo -e "  ${CYAN}-${NC} $1"; }

# ----------------------------- configuracion -----------------------------
INSTALL_DIR="${INSTALL_DIR:-/opt/teaspeak}"
SERVICE_USER="${SERVICE_USER:-teaspeak}"
DB_NAME="${DB_NAME:-teaspeak}"
DB_LOGS_NAME="${DB_LOGS_NAME:-teaspeak_logs}"
DB_USER="${DB_USER:-teaspeak}"
DB_CONNECTIONS="${DB_CONNECTIONS:-8}"
LOGS_CAP_GB="${LOGS_CAP_GB:-25}"
# Cuando se ejecuta con "curl ... | bash", $0 es "bash" y no hay directorio propio;
# por eso SCRIPT_DIR es solo un punto de partida y el trabajo real usa ASSET_DIR (mas
# abajo), que apunta a donde esten de verdad el bundle y los scripts auxiliares.
SCRIPT_DIR="$(cd "$(dirname "$0" 2>/dev/null)" 2>/dev/null && pwd || pwd)"
BUNDLE_DIR="${BUNDLE_DIR:-${SCRIPT_DIR}/bundle}"
ASSET_DIR="${SCRIPT_DIR}"

# --- Distribucion por GitHub (modelo curl, sin scp) -------------------------
# BUNDLE_URL: tarball .tar.gz (p. ej. un asset de un Release de GitHub) que contiene
#   el directorio bundle/ y los scripts (firewall.sh, logs_retention.sh, backup.sh).
#   Si se define, install.sh lo descarga con curl y trabaja desde ahi. Puede veni
#   por variable de entorno o quedar "horneada" aqui abajo.
BUNDLE_URL="${BUNDLE_URL:-}"

# protocol_key.txt es SENSIBLE y NO viaja en el bundle publico. Vias de entrega, po
# orden de preferencia (la primera que exista gana):
#   PROTOCOL_KEY_B64  -> la clave en base64, pegada en la linea de comandos (sin fichero)
#   PROTOCOL_KEY_URL  -> descargar de una URL privada con curl
#   PROTOCOL_KEY_PATH -> ruta a un fichero local
#   (o si el bundle es privado e incluye bundle/protocol_key.txt, se usa ese)
PROTOCOL_KEY_B64="${PROTOCOL_KEY_B64:-}"
PROTOCOL_KEY_URL="${PROTOCOL_KEY_URL:-}"
PROTOCOL_KEY_PATH="${PROTOCOL_KEY_PATH:-${SCRIPT_DIR}/protocol_key.txt}"
# ------------------------------------------------------------------------

[[ $EUID -ne 0 ]] && err "Ejecuta como root: sudo ./install.sh"

clea
echo -e "${CYAN}${BOLD}================================================"
echo -e "     INSTALADOR TEASPEAK + POSTGRESQL (Debian 11)"
echo -e "================================================${NC}"

# --- 0. Comprobaciones ---
step "Comprobaciones previas"
if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    info "SO: ${PRETTY_NAME:-desconocido}"
    [[ "${VERSION_ID:-}" != "11" ]] && echo -e "  ${YELLOW}Aviso: probado en Debian 11; otras versiones pueden variar.${NC}"
fi
# Descargar el desplegable si no esta en local y hay URL (modelo GitHub/curl).
# El tarball contiene bundle/ + los scripts auxiliares; se extrae a un dir temporal
# y ASSET_DIR pasa a apuntar ahi, de modo que "curl | bash" funcione sin scp.
if [[ ! -d "${BUNDLE_DIR}" && -n "${BUNDLE_URL}" ]]; then
    info "Descargando desplegable desde ${BUNDLE_URL}"
    apt-get install -y -qq curl tar >/dev/null 2>&1 || true
    WORK="$(mktemp -d)"
    tmp_tar="${WORK}/deploy.tar.gz"
    curl -fSL "${BUNDLE_URL}" -o "${tmp_tar}" || err "no se pudo descargar el desplegable"
    tar -xzf "${tmp_tar}" -C "${WORK}" || err "no se pudo extraer el desplegable"
    rm -f "${tmp_tar}"
    # el tarball puede traer bundle/ en la raiz o dentro de un subdirectorio
    found="$(find "${WORK}" -maxdepth 3 -type d -name bundle | head -1)"
    [[ -n "${found}" ]] || err "el tarball descargado no contiene un directorio bundle/"
    BUNDLE_DIR="${found}"
    ASSET_DIR="$(dirname "${found}")"   # aqui viven firewall.sh, logs_retention.sh, backup.sh
fi

[[ -d "${BUNDLE_DIR}" ]] || err "No existe el bundle en ${BUNDLE_DIR}. Genera uno con make_bundle.sh (o define BUNDLE_URL)."
[[ -f "${BUNDLE_DIR}/TeaSpeakServer" ]] || err "Falta ${BUNDLE_DIR}/TeaSpeakServer"
[[ -f "${BUNDLE_DIR}/libteaspeak_rtc.so" ]] || err "Falta ${BUNDLE_DIR}/libteaspeak_rtc.so"
[[ -f "${BUNDLE_DIR}/config.yml" ]] || err "Falta ${BUNDLE_DIR}/config.yml"
log "Bundle presente en ${BUNDLE_DIR}"

# --- 1. Dependencias de runtime (NO de compilacion) ---
step "Instalando dependencias de runtime"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    postgresql postgresql-client libpq5 \
    libnice10 zlib1g ca-certificates \
    iptables iptables-persistent \
    xz-utils curl >/dev/null 2>&1 || err "fallo instalando dependencias"
log "Dependencias instaladas"

# --- 2. Usuario de servicio ---
step "Usuario de servicio '${SERVICE_USER}'"
if id "${SERVICE_USER}" &>/dev/null; then
    info "ya existe"
else
    useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
    log "creado"
fi

# --- 3. PostgreSQL: arranque + usuario + bases ---
step "Configurando PostgreSQL"
systemctl enable postgresql >/dev/null 2>&1
systemctl start postgresql
sleep 2
[[ "$(systemctl is-active postgresql)" == "active" ]] || err "PostgreSQL no arranco"
info "PostgreSQL activo ($(sudo -u postgres psql -tAc 'SHOW server_version' 2>/dev/null | tr -d ' '))"

DB_PASS="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"

# Crear/actualizar el rol (idempotente). Las bases se crean aparte mas abajo
# porque CREATE DATABASE no admite IF NOT EXISTS ni va dentro de un bloque DO.
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL >/dev/null 2>&1 || err "fallo creando el rol en PostgreSQL"
DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';
    ELSE
        ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';
    END IF;
END \$\$;
SQL

# Crear las bases si no existen (CREATE DATABASE no admite IF NOT EXISTS)
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} WITH OWNER ${DB_USER} ENCODING 'UTF8';" >/dev/null
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_LOGS_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_LOGS_NAME} WITH OWNER ${DB_USER} ENCODING 'UTF8';" >/dev/null
log "Rol '${DB_USER}' y bases '${DB_NAME}', '${DB_LOGS_NAME}' listas (PostgreSQL escucha solo en localhost)"

MAIN_URL="postgres://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}?connections=${DB_CONNECTIONS}"
LOGS_URL="postgres://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_LOGS_NAME}?connections=4"

# --- 4. Desplegar el bundle ---
step "Desplegando el servidor en ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
cp -a "${BUNDLE_DIR}/." "${INSTALL_DIR}/"
mkdir -p "${INSTALL_DIR}/logs" "${INSTALL_DIR}/files" "${INSTALL_DIR}/crash_dumps" "${INSTALL_DIR}/backups"
chmod +x "${INSTALL_DIR}/TeaSpeakServer"
log "Ficheros copiados"

# protocol_key.txt: si el bundle era privado, "cp -a bundle/." ya lo dejo en su sitio.
# Si no (bundle publico), se inyecta por B64 (pegado), URL privada o ruta local.
if [[ ! -f "${INSTALL_DIR}/protocol_key.txt" ]]; then
    if [[ -n "${PROTOCOL_KEY_B64}" ]]; then
        if echo "${PROTOCOL_KEY_B64}" | base64 -d > "${INSTALL_DIR}/protocol_key.txt" 2>/dev/null; then
            info "protocol_key.txt tomado de PROTOCOL_KEY_B64"
        else
            rm -f "${INSTALL_DIR}/protocol_key.txt"
            echo -e "  ${YELLOW}PROTOCOL_KEY_B64 no es base64 valido; se ignora${NC}"
        fi
    elif [[ -n "${PROTOCOL_KEY_URL}" ]]; then
        curl -fSL "${PROTOCOL_KEY_URL}" -o "${INSTALL_DIR}/protocol_key.txt" && info "protocol_key.txt descargado" \
            || echo -e "  ${YELLOW}No se pudo descargar el protocol_key.txt${NC}"
    elif [[ -f "${PROTOCOL_KEY_PATH}" ]]; then
        cp "${PROTOCOL_KEY_PATH}" "${INSTALL_DIR}/protocol_key.txt"
        info "protocol_key.txt tomado de ${PROTOCOL_KEY_PATH}"
    elif [[ -f "${ASSET_DIR}/protocol_key.txt" ]]; then
        cp "${ASSET_DIR}/protocol_key.txt" "${INSTALL_DIR}/protocol_key.txt"
        info "protocol_key.txt tomado de ${ASSET_DIR}"
    fi
fi
if [[ -f "${INSTALL_DIR}/protocol_key.txt" ]]; then
    log "protocol_key.txt presente"
else
    echo -e "  ${YELLOW}AVISO: no hay protocol_key.txt. El servidor generara uno nuevo, pero los${NC}"
    echo -e "  ${YELLOW}clientes TeamSpeak NO conectaran hasta poner el correcto en ${INSTALL_DIR}/protocol_key.txt${NC}"
fi

# --- 5. Configurar config.yml (apuntar a PostgreSQL) ---
step "Configurando la base de datos en config.yml"
python3 - "${INSTALL_DIR}/config.yml" "${MAIN_URL}" "${LOGS_URL}" <<'PYEOF'
import sys, re
path, main_url, logs_url = sys.argv[1], sys.argv[2], sys.argv[3]
c = open(path).read()

# base principal
if re.search(r'\n    url:', c):
    c = re.sub(r'(\n    url:).*', r'\1 ' + main_url, c, count=1)

# log.instance.{database_url,ignore_query_actions}
if 'database_url:' in c:
    c = re.sub(r'(\n    database_url:).*', r'\1 ' + logs_url, c, count=1)
else:
    c = re.sub(r'(\nlog:\n)', r'\1  instance:\n    database_url: ' + logs_url + '\n    ignore_query_actions: 1\n', c, count=1)

open(path, 'w').write(c)
print("  config.yml actualizado")
PYEOF
log "config.yml apunta a PostgreSQL (main + logs)"

# proteger secretos
[[ -f "${INSTALL_DIR}/protocol_key.txt" ]] && chmod 600 "${INSTALL_DIR}/protocol_key.txt"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 750 "${INSTALL_DIR}"

# --- 6. Servicio systemd (con anticrash) ---
step "Instalando el servicio systemd"
cat > /etc/systemd/system/teaspeak.service <<EOF
[Unit]
Description=TeaSpeak Server (PostgreSQL)
After=network-online.target postgresql.service
Wants=network-online.target
Requires=postgresql.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=LD_LIBRARY_PATH=${INSTALL_DIR}:${INSTALL_DIR}/libs
ExecStart=${INSTALL_DIR}/TeaSpeakServe
# Anticrash: reinicio automatico ante cualquier caida (sustituye al cron antiguo)
Restart=always
RestartSec=5
StartLimitIntervalSec=0
LimitNOFILE=65536
# Endurecimiento
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable teaspeak.service >/dev/null 2>&1
log "Servicio 'teaspeak' instalado (Restart=always como anticrash)"

# --- 7. Scripts de mantenimiento + cron ---
step "Instalando retencion de logs y backups"
for s in logs_retention.sh backup.sh; do
    if [[ -f "${ASSET_DIR}/${s}" ]]; then
        install -m 755 "${ASSET_DIR}/${s}" "${INSTALL_DIR}/${s}"
    else
        echo -e "  ${YELLOW}${s} no encontrado en ${ASSET_DIR}; el cron correspondiente no funcionara${NC}"
    fi
done

cat > /etc/cron.d/teaspeak <<EOF
# Retencion FIFO del log de instancia (cada 30 min)
TEASPEAK_LOGS_DB=${DB_LOGS_NAME}
TEASPEAK_LOGS_CAP_GB=${LOGS_CAP_GB}
*/30 * * * * root ${INSTALL_DIR}/logs_retention.sh
# Backup diario a las 06:00
TEASPEAK_DIR=${INSTALL_DIR}
TEASPEAK_DB=${DB_NAME}
0 6 * * * root ${INSTALL_DIR}/backup.sh
EOF
chmod 644 /etc/cron.d/teaspeak
log "Cron instalado: retencion cada 30 min (tope ${LOGS_CAP_GB} GB), backup diario 06:00"

# --- 8. Firewall ---
step "Aplicando firewall"
if [[ "${SKIP_FIREWALL:-0}" == "1" ]]; then
    echo -e "  ${YELLOW}Omitido (SKIP_FIREWALL=1). Aplicalo luego con: sudo bash firewall.sh${NC}"
    echo -e "  ${YELLOW}Revisa antes las whitelists de SSH y Query en firewall.sh.${NC}"
elif [[ -f "${ASSET_DIR}/firewall.sh" ]]; then
    # copia el firewall al INSTALL_DIR para poder reaplicarlo luego a mano
    install -m 755 "${ASSET_DIR}/firewall.sh" "${INSTALL_DIR}/firewall.sh" 2>/dev/null || true
    bash "${ASSET_DIR}/firewall.sh" || echo -e "  ${YELLOW}El firewall devolvio error; revisa firewall.sh${NC}"
else
    echo -e "  ${YELLOW}firewall.sh no encontrado; omitido${NC}"
fi

# --- 9. Arranque ---
step "Arrancando TeaSpeak"
systemctl start teaspeak.service
sleep 12
if [[ "$(systemctl is-active teaspeak.service)" == "active" ]]; then
    log "Servicio activo"
else
    echo -e "  ${YELLOW}El servicio no esta activo; revisa: journalctl -u teaspeak -n 50${NC}"
fi

# credenciales del primer arranque
CREDS=$(journalctl -u teaspeak.service --no-pager 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -iE 'Username:|Password:|serveradmin token' | tail -5)

echo ""
echo -e "${GREEN}${BOLD}================ INSTALACION COMPLETADA ================${NC}"
echo -e "  Directorio:  ${BOLD}${INSTALL_DIR}${NC}"
echo -e "  Base datos:  PostgreSQL local (${DB_NAME} + ${DB_LOGS_NAME})"
echo -e "  Servicio:    systemctl {status,restart,stop} teaspeak"
echo -e "  Logs:        journalctl -u teaspeak -f"
echo -e "  Retencion:   ${LOGS_CAP_GB} GB (FIFO, cada 30 min)"
echo -e "  Backups:     ${INSTALL_DIR}/backups (diario 06:00)"
echo ""
if [[ -n "${CREDS}" ]]; then
    echo -e "${CYAN}Credenciales del primer arranque (guardalas):${NC}"
    echo "${CREDS}"
else
    echo -e "${YELLOW}No se detectaron credenciales en el log (¿la BD ya estaba poblada?).${NC}"
    echo -e "Puedes consultarlas en:  journalctl -u teaspeak | grep -iE 'Username|Password|token'"
fi
echo ""
echo -e "${YELLOW}IMPORTANTE:${NC} revisa las whitelists y el rango de puertos de voz en firewall.sh."
