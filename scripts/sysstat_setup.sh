#!/bin/bash
# =============================================================================
# sysstat (sar) para forense de incidentes en la maquina de TeaSpeak.
#
# Deja registradas CPU, memoria, red (incluidos errores UDP: sar -n UDP,EDEV),
# disco y carga con muestreo CADA MINUTO y 28 dias de historico, para poder
# reconstruir despues que paso durante un corte/oleada de reconexiones
# (p. ej.  sar -n UDP -s 21:00:00 -e 21:30:00  o  sar -n EDEV -f /var/log/sysstat/saDD).
#
# - instala el paquete sysstat si falta
# - /etc/default/sysstat: ENABLED="true"
# - /etc/sysstat/sysstat: HISTORY=28
# - override del timer: /etc/systemd/system/sysstat-collect.timer.d/every-minute.conf
#   (por defecto Debian muestrea cada 10 min)
# - habilita sysstat + sysstat-collect.timer
#
# Idempotente: se puede relanzar sin efectos secundarios. Coste despreciable
# (~1 proceso sadc por minuto; ~ unos MB/dia en /var/log/sysstat).
# =============================================================================
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "[sysstat_setup] ejecuta como root." >&2; exit 1; }

HISTORY_DAYS="${SYSSTAT_HISTORY:-28}"

if ! dpkg -s sysstat >/dev/null 2>&1; then
    echo "[sysstat_setup] instalando sysstat..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sysstat >/dev/null || {
        apt-get update -qq >/dev/null 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sysstat >/dev/null
    } || { echo "[sysstat_setup] no pude instalar sysstat." >&2; exit 1; }
fi

# activar la recoleccion
DEF=/etc/default/sysstat
if [ -f "$DEF" ] && grep -q '^ENABLED=' "$DEF"; then
    sed -i 's/^ENABLED=.*/ENABLED="true"/' "$DEF"
else
    echo 'ENABLED="true"' >> "$DEF"
fi

# historico (dias)
CFG=/etc/sysstat/sysstat
if [ -f "$CFG" ]; then
    if grep -q '^HISTORY=' "$CFG"; then
        sed -i "s/^HISTORY=.*/HISTORY=${HISTORY_DAYS}/" "$CFG"
    else
        echo "HISTORY=${HISTORY_DAYS}" >> "$CFG"
    fi
fi

# muestreo cada minuto (el OnCalendar= vacio anula el */10 de Debian)
OVR_DIR=/etc/systemd/system/sysstat-collect.timer.d
mkdir -p "$OVR_DIR"
cat > "$OVR_DIR/every-minute.conf" <<'EOF'
[Timer]
OnCalendar=
OnCalendar=*:*:00
EOF

systemctl daemon-reload
systemctl enable --now sysstat >/dev/null 2>&1 || true
systemctl enable --now sysstat-collect.timer >/dev/null 2>&1 || true
systemctl restart sysstat-collect.timer >/dev/null 2>&1 || true

echo "[sysstat_setup] sysstat activo: muestreo cada minuto, historico ${HISTORY_DAYS} dias (/var/log/sysstat)."
