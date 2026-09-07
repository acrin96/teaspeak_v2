#!/bin/bash
# =============================================================================
# Tuning de red para TeaSpeak (voz UDP).
#
# Sube el buffer de recepcion UDP (net.core.rmem_max/default), el de envio y el
# backlog del kernel (netdev_max_backlog) para absorber rafagas y el jitter del
# hilo de voz. Sin esto, con muchos clientes el buffer por defecto de Linux
# (~208 KB) se desborda y el kernel descarta paquetes de voz: en el cliente eso
# aparece como "You dropped (Packet Resend Failed)" y en los logs como
# "receive buffer errors" (netstat -su) + "VoiceClient::tick needs more than...".
#
# Idempotente. El cap por-socket aplica a sockets NUEVOS, asi que el efecto pleno
# llega cuando TeaSpeak re-crea sus sockets (reinicio del servicio).
# =============================================================================
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "[sysctl_tuning] ejecuta como root." >&2; exit 1; }

CONF=/etc/sysctl.d/99-teaspeak-voice.conf
cat > "$CONF" <<'EOF'
# Gestionado por teaspeak_v2 (scripts/sysctl_tuning.sh). Buffers de red para la voz UDP.
net.core.rmem_max = 16777216
net.core.rmem_default = 1048576
net.core.wmem_max = 4194304
net.core.wmem_default = 1048576
net.core.netdev_max_backlog = 5000
EOF

sysctl -p "$CONF" >/dev/null 2>&1 || sysctl --system >/dev/null 2>&1 || true
echo "[sysctl_tuning] buffers UDP de voz aplicados ($CONF). El efecto pleno requiere reiniciar TeaSpeak."
