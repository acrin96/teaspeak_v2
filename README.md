# TeaSpeak v2 — distribución

Fork de **TeaSpeak 1.4.21-beta-3** migrado a **PostgreSQL**, endurecido y listo para arrancar de fábrica en
**Debian 11**. Este repositorio contiene solo lo necesario para instalar y operar (binario + scripts); el código
fuente es privado.

## Instalación en un comando

En un Debian 11 limpio, como `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/acrin96/teaspeak_v2/main/install.sh | sudo bash
```

Ese único comando lo hace **todo**: descarga el binario, instala dependencias, crea el rol y las dos bases
PostgreSQL (principal + logs), despliega en `/opt/teaspeak`, genera la configuración, instala los scripts
(`firewall.sh`, `backup.sh`, `logs_retention.sh`), programa el backup diario y la retención de logs, registra
el servicio `systemd`, arranca el servidor **e imprime al final la contraseña de `serveradmin` y la clave de
privilegio del grupo Server Admin**. Es **idempotente**: re-ejecútalo para actualizar sin perder config ni datos.

> **Con la clave de protocolo de tu servidor:** deja tu `protocol_key.txt` (sin formato) en `/root/`
> **antes** de instalar; el instalador la detecta y la usa:
> ```bash
> # copia tu protocol_key.txt a /root/protocol_key.txt y luego:
> curl -fsSL https://raw.githubusercontent.com/acrin96/teaspeak_v2/main/install.sh | bash
> ```
> (Alternativa: `PROTOCOL_KEY_B64="$(base64 -w0 protocol_key.txt)" bash install.sh`.)

Si más adelante necesitas las credenciales de nuevo:

```bash
cd /tmp
sudo -u postgres psql -d teaspeak -c "SELECT username,password FROM queries;"   # serveradmin
sudo -u postgres psql -d teaspeak -c "SELECT token,description FROM tokens;"     # privilege key
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
