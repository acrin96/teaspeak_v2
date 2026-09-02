# TeaSpeak v2 — distribución

Fork de **TeaSpeak 1.4.21-beta-3** migrado a **PostgreSQL**, endurecido y listo para arrancar de fábrica en
**Debian 11**. Este repositorio contiene solo lo necesario para instalar y operar (binario + scripts); el código
fuente es privado.

## Instalación en un comando

En un Debian 11 limpio, como `root`:

```bash
cd /root
curl -fsSLO https://github.com/acrin96/teaspeak_v2/releases/latest/download/teaspeak_v2_1.4.21-beta-3_linux_amd64.tar.gz
tar -xzf teaspeak_v2_1.4.21-beta-3_linux_amd64.tar.gz
cd teaspeak_v2_bundle
sudo bash ./install.sh
```

El instalador instala dependencias, crea el rol y las dos bases PostgreSQL (principal + logs), despliega en
`/opt/teaspeak`, genera la configuración, registra el servicio `systemd` y el cron de retención, y arranca el
servidor. Es **idempotente**: re-ejecútalo para actualizar sin perder config ni datos.

La contraseña inicial de `serveradmin` y la clave de privilegio del grupo Server Admin aparecen en el log:

```bash
journalctl -u teaspeak | grep -iE 'serveradmin|token|privilege'
```

## Scripts

| Script | Para qué |
|---|---|
| `install.sh` | Instalador / actualizador. |
| `scripts/firewall.sh` | Firewall iptables: SSH, ServerQuery y PostgreSQL solo para tu whitelist; voz y ficheros públicos con rate-limit. |
| `scripts/backup.sh` | Backup con `pg_dump` de la base principal + ficheros de runtime, con retención. Ideal para cron diario. |
| `scripts/logs_retention.sh` | Tope FIFO de tamaño para la base de logs (lo instala `install.sh` en cron horario). |

## La `protocol_key`

Es la identidad del servidor y es **privada**: no está en este repositorio. Apórtala al instalar con
`PROTOCOL_KEY_B64` (contenido en base64) o cópiala manualmente a `/opt/teaspeak/protocol_key.txt` (`chmod 600`).

## Manual

Manual completo de instalación y uso: ver la sección *Releases* / el enlace del manual.

## Notas

- Plataforma soportada: **Debian 11** (bullseye), amd64.
- La corrección anti-crash del handshake (validación DER) va **compilada en el binario**; ya no hace falta el
  antiguo script de mitigación por iptables.
- El código fuente con todos los parches se mantiene en un repositorio privado aparte.
