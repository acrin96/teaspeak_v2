#!/bin/bash
# =============================================================================
# Firewall (iptables) para TeaSpeak v2  -  PostgreSQL / Debian 11
#
#   - SSH (22), ServerQuery (10101) y PostgreSQL (5432): CERRADOS al publico,
#     abiertos solo a las IPs de whitelist.
#   - Voz (UDP) y transferencia de ficheros (TCP): publicos con rate-limit.
#   - Si WHITELIST_DB no esta vacio, ademas de abrir 5432 a esas IPs configura
#     PostgreSQL para escuchar de forma remota SOLO desde ellas (listen_addresses
#     + pg_hba). Deja WHITELIST_DB vacio para mantener la BD solo en localhost.
#   - Rate-limit UDP con hashlimit (limita conexiones NUEVAS por IP sin afectar
#     al trafico de voz ya establecido).
#
# Edita el bloque CONFIGURACION o pasa las variables por entorno.
# =============================================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log_ok()   { echo -e "  [${GREEN}OK${NC}] $1"; }
log_info() { echo -e "  [${CYAN}INFO${NC}] $1"; }
step()     { echo -e "\n${CYAN}>>>${NC} $1"; }

[[ $EUID -ne 0 ]] && { echo -e "${RED}[ERROR] Ejecuta como root.${NC}"; exit 1; }

# ============================ CONFIGURACION ============================
# IPs con acceso a SSH (22). Separadas por espacios. Vacio = abierto a todos (no recomendado).
WHITELIST_SSH="${WHITELIST_SSH:-172.216.237.49}"

# IPs con acceso al ServerQuery (10101). El query queda CERRADO al publico.
WHITELIST_QUERY="${WHITELIST_QUERY:-172.216.237.49 23.26.135.58}"

# IPs con acceso a PostgreSQL (5432) de forma remota. Vacio = BD solo en localhost.
WHITELIST_DB="${WHITELIST_DB:-172.216.237.49}"

# IPs bloqueadas explicitamente en la frontera.
BLACKLIST_IPS="${BLACKLIST_IPS:-177.26.255.242 172.59.187.147 172.59.188.223 172.59.189.210 172.56.16.242 172.59.190.249}"

# Rango de puertos de VOZ (UDP). Debe cubrir los puertos de tus servidores virtuales.
UDP_PORTS="${UDP_PORTS:-10200:10225}"

TCP_FILES="${TCP_FILES:-30303}"    # transferencia de ficheros
TCP_QUERY="${TCP_QUERY:-10101}"    # ServerQuery
TCP_DB="${TCP_DB:-5432}"           # PostgreSQL
SSH_PORT="${SSH_PORT:-22}"

FILES_CONNLIMIT="${FILES_CONNLIMIT:-10}"   # conexiones concurrentes por IP en ficheros
VOICE_NEW_PER_SEC="${VOICE_NEW_PER_SEC:-10}"
VOICE_NEW_BURST="${VOICE_NEW_BURST:-30}"
# ======================================================================

clear
echo -e "${CYAN}${BOLD}========================================================"
echo -e "        FIREWALL TEASPEAK v2 (PostgreSQL / Debian 11)"
echo -e "========================================================${NC}"

step "Politicas por defecto (rechazar todo lo entrante no autorizado)"
iptables -F; iptables -X
iptables -P INPUT DROP; iptables -P FORWARD DROP; iptables -P OUTPUT ACCEPT
log_ok "INPUT/FORWARD = DROP, OUTPUT = ACCEPT"

step "Loopback y estado de conexiones"
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate INVALID -j DROP
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
log_ok "Establecidas/relacionadas permitidas; invalidas descartadas"

step "Blacklist"
for ip in $BLACKLIST_IPS; do
    iptables -A INPUT -s "$ip" -j DROP
    echo -e "  - ${RED}$ip${NC} bloqueada"
done

step "SSH ($SSH_PORT) - solo whitelist"
if [[ -z "${WHITELIST_SSH// }" ]]; then
    iptables -A INPUT -p tcp --dport "$SSH_PORT" -j ACCEPT
    log_info "SSH ABIERTO a todos (no hay whitelist definida)"
else
    for ip in $WHITELIST_SSH; do
        iptables -A INPUT -p tcp -s "$ip" --dport "$SSH_PORT" -j ACCEPT
        echo -e "  - SSH permitido a ${YELLOW}$ip${NC}"
    done
fi

step "ServerQuery ($TCP_QUERY) - solo whitelist"
for ip in $WHITELIST_QUERY; do
    iptables -A INPUT -p tcp -s "$ip" --dport "$TCP_QUERY" -j ACCEPT
    echo -e "  - Query permitido a ${YELLOW}$ip${NC}"
done

step "PostgreSQL ($TCP_DB) - solo whitelist (o localhost si esta vacio)"
if [[ -z "${WHITELIST_DB// }" ]]; then
    log_info "WHITELIST_DB vacio: PostgreSQL permanece solo en localhost."
else
    for ip in $WHITELIST_DB; do
        iptables -A INPUT -p tcp -s "$ip" --dport "$TCP_DB" -j ACCEPT
        echo -e "  - PostgreSQL permitido a ${YELLOW}$ip${NC}"
    done
    # --- habilitar escucha remota de PostgreSQL SOLO para esas IPs ---
    PG_CONF="$(find /etc/postgresql -name postgresql.conf 2>/dev/null | head -1)"
    PG_HBA="$(find /etc/postgresql -name pg_hba.conf 2>/dev/null | head -1)"
    if [[ -n "$PG_CONF" && -n "$PG_HBA" ]]; then
        PG_NEEDS_RESTART=0
        if ! grep -qE "^[[:space:]]*listen_addresses[[:space:]]*=[[:space:]]*'\\*'" "$PG_CONF"; then
            sed -i "s/^[[:space:]]*#\?[[:space:]]*listen_addresses[[:space:]]*=.*/listen_addresses = '*'/" "$PG_CONF"
            grep -qE "^listen_addresses" "$PG_CONF" || echo "listen_addresses = '*'" >> "$PG_CONF"
            PG_NEEDS_RESTART=1   # listen_addresses solo aplica tras un restart (no reload)
        fi
        # usar el MISMO metodo de auth que la entrada local (127.0.0.1); si el password del rol
        # esta guardado como md5, una entrada scram-sha-256 falla ("password authentication failed").
        PG_METHOD="$(awk '/^host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1\/32/{print $5; exit}' "$PG_HBA")"
        [ -z "$PG_METHOD" ] && PG_METHOD=md5
        for ip in $WHITELIST_DB; do
            if ! grep -qE "^host[[:space:]]+all[[:space:]]+all[[:space:]]+$ip/32" "$PG_HBA"; then
                echo "host    all    all    $ip/32    $PG_METHOD" >> "$PG_HBA"
                PG_NEEDS_RESTART=1
            fi
        done
        if [[ "$PG_NEEDS_RESTART" = 1 ]]; then
            systemctl restart postgresql 2>/dev/null || log_info "reinicia postgresql a mano para aplicar los cambios"
        fi
        log_ok "PostgreSQL escucha remoto habilitado solo para: $WHITELIST_DB"
    else
        log_info "No encontre postgresql.conf/pg_hba.conf; la regla iptables queda, pero habilita el listen manualmente."
    fi
fi

step "Voz UDP ($UDP_PORTS) - publico con rate-limit de conexiones nuevas"
iptables -A INPUT -p udp --dport "$UDP_PORTS" -m conntrack --ctstate NEW \
    -m hashlimit --hashlimit-name ts_voice --hashlimit-mode srcip \
    --hashlimit-above "${VOICE_NEW_PER_SEC}/sec" --hashlimit-burst "${VOICE_NEW_BURST}" -j DROP
iptables -A INPUT -p udp --dport "$UDP_PORTS" -j ACCEPT
log_ok "Voz abierta; max ${VOICE_NEW_PER_SEC} conexiones nuevas/seg por IP (rafaga ${VOICE_NEW_BURST})"

step "Transferencia de ficheros ($TCP_FILES) - publico con limite de conexiones"
iptables -A INPUT -p tcp --dport "$TCP_FILES" -m connlimit --connlimit-above "${FILES_CONNLIMIT}" -j REJECT
iptables -A INPUT -p tcp --dport "$TCP_FILES" -j ACCEPT
log_ok "Ficheros abiertos; max ${FILES_CONNLIMIT} conexiones concurrentes por IP"

step "ICMP (ping) limitado"
iptables -A INPUT -p icmp --icmp-type echo-request -m limit --limit 1/s --limit-burst 5 -j ACCEPT
iptables -A INPUT -p icmp -j DROP
log_ok "Ping limitado a 1/seg (rafaga 5)"

step "Persistiendo reglas"
mkdir -p /etc/iptables
if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1
else
    iptables-save > /etc/iptables/rules.v4
fi
log_ok "Reglas guardadas (se restauran al arrancar)"

echo ""
echo -e "${GREEN}${BOLD}Firewall aplicado.${NC}"
log_info "SSH ($SSH_PORT), Query ($TCP_QUERY) y PostgreSQL ($TCP_DB): solo whitelist."
log_info "Voz ($UDP_PORTS) y Ficheros ($TCP_FILES): publicos con rate-limit."
echo -e "${YELLOW}Recuerda: UDP_PORTS ($UDP_PORTS) debe cubrir los puertos de tus servidores virtuales.${NC}"
echo -e "${YELLOW}Aviso: si te conectas por SSH desde una IP fuera de WHITELIST_SSH, te quedaras fuera.${NC}"
