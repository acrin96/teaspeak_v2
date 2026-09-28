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
(`firewall.sh`, `backup.sh`, `logs_retention.sh`, `sysctl_tuning.sh`, `sysstat_setup.sh`), programa el backup
diario y la retención de logs, aplica el tuning de red y activa `sysstat` (sar cada minuto), registra
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
| `scripts/firewall.sh` | Firewall iptables: SSH, ServerQuery y PostgreSQL solo para tu whitelist; voz pública fuera de conntrack (`VOICE_NOTRACK=1`); ficheros públicos con límite por IP; WireGuard (red privada 10.66.0.0/24, `WG_PEERS`); paneles 8443 (admin) y 443 (Cloudflare). `NO_PERSIST=1` para probarlo en un network namespace. |
| `scripts/sysctl_tuning.sh` | Tuning de red: sube los buffers UDP (recepción `rmem_default` 4 MB / `rmem_max` 32 MB; envío `wmem_default` 8 MB / `wmem_max` 16 MB) y el backlog del kernel para evitar "receive buffer errors" (Packet Resend Failed) bajo carga y los `EAGAIN` de envío UDP en las oleadas de reconexión. |
| `scripts/sysstat_setup.sh` | Activa `sysstat` (sar) para forense de incidentes: muestreo **cada minuto** (override del `sysstat-collect.timer`) y **28 días** de histórico en `/var/log/sysstat`. Instala el paquete si falta. Idempotente; `install.sh` lo aplica (omitir con `APPLY_SYSSTAT=0`). Ej.: `sar -n UDP,EDEV -s 21:00:00 -e 21:30:00`. |
| `scripts/backup.sh` | Backup con `pg_dump` de la base principal + ficheros de runtime, con retención. Ideal para cron diario. |
| `scripts/logs_retention.sh` | Tope FIFO de tamaño para la base de logs (lo instala `install.sh` en cron horario). |
| `scripts/ts_maint.py` | Mantenimiento one-shot (cron `/etc/cron.d/ts-maint-once`): sube los pools de hilos de `config.yml`, reinicia, verifica salud y hace rollback automático; avisa por poke + WhatsApp al admin. `--check` = ensayo sin tocar producción. **Superado:** la ventana del 29-sep-2026 baja los hilos a la variante tuned con `ts_maint_build2.py`; no volver a ejecutarlo. |
| `scripts/ts_maint_bin.py` | Mantenimiento one-shot de **cambio de binario** (cron `/etc/cron.d/ts-maint-bin-once`, ventana 10:02): valida el md5 del build nuevo, backup del binario + `config.yml` + `pg_dump`, cambia el binario, health-check y rollback automático. Reutiliza las funciones de `ts_maint.py`. `--check` = ensayo. |
| `scripts/ts_maint_build1.py` | Mantenimiento de **Build 1** (28-sep-2026) en un solo reinicio: binario Build 1 + rotaciÃ³n de la contraseÃ±a del rol PostgreSQL `teaspeak` (B15: se genera en el script, nunca se imprime, `ALTER ROLE` con TeaSpeak parado justo antes de arrancar; se actualiza en `general.database.url` y `log.instance_logs_url`) + `server.clients.teaspeak: 0`. Rutina poke + 5 WhatsApp al admin, backup (binario, `config.yml`, verificador del rol, `pg_dump`), health-check, reinicio del bot, post-checks leÃ­dos de `/opt/teaspeak/logs` y del log de PG, y rollback automÃ¡tico de binario + config + contraseÃ±a. **Sin cron**: se lanza con `systemd-run`. `--check` = ensayo completo (incluye el parseo en seco del config editado) sin ALTER, sin reinicio y sin WhatsApp. |
| `scripts/ts_maint_build2.py` | Mantenimiento de **Build 2** (rendimiento T13-T17 + T11 + crashes T21/T22; `docs/BUILD2.md` de `teaspeak_v2-src`) + **hilos tuned** en `config.yml` (`ticking 2`, `command_execute 4`, `network_events 2`, `voice.io_min 4`, `voice.io_limit 4`; 85 → ~59 hilos) en un solo reinicio. Exige Build 1 en vivo (`ef2c6095`) y el binario `ae2a571c`. Rutina poke ("TsBot Alert", SPY excluidos) + 5 WhatsApp al admin, backup del binario + `config.yml` + `pg_dump`, health-check, reinicio del bot, post-checks (log de PG, crashes, parada limpia de Build 1, `synchronous_commit: off`, hilos, bots reconectados) y rollback automatico a Build 1 + config original. Programado para el 29-sep-2026 con cron de sistema one-shot (`/etc/cron.d/ts-maint-build2-once`, 09:57). `--check` = ensayo sin reinicio, sin escribir el config y sin WhatsApp. |
| `scripts/post_build2.py` | Paso posterior de la ventana Build 2 (cron one-shot 10:20): si el mantenimiento termina OK fusiona las ramas en `main` y publica la release `v1.4.21-beta-3-build2` (latest, mismos assets que build1) verificando la descarga; **siempre** manda al admin un WhatsApp final con todo lo aplicado, punto por punto. `--dry` = sin WhatsApp ni escrituras en GitHub; `--publish-only` = solo la publicacion. |
| `scripts/stage_release_build2.sh` | Prepara (sin publicar) los assets de la release build2: bundle de build1 con el binario Build 2 y la seccion `threads` de `config.template.yml` igual a la de prod; geoloc sin cambios; SHA256SUMS. |
| `scripts/vtests_build1.py` + `scripts/run_vtests.sh` | Pruebas V1â€“V6, V8 y apoyo a V9 de Build 1 (`docs/BUILD1.md` de `teaspeak_v2-src`) en un vserver desechable `zz-e2e-life` creado y borrado con el ciclo de vida de ops (`ops_lifecycle_enabled` 1 â†’ 0). Solo lectura contra los demÃ¡s vservers. `run_vtests.sh --check | run | voice | cleanup`. |

## Firewall y acceso a PostgreSQL (pgAdmin)

El instalador **aplica el firewall por defecto** (`scripts/firewall.sh`, whitelist en su cabecera): SSH,
ServerQuery y PostgreSQL quedan abiertos solo a tus IPs; voz pública sin conntrack y ficheros públicos con límite de conexiones por IP. Si
`WHITELIST_DB` tiene tu IP, además habilita el acceso remoto a PostgreSQL y podrás conectarte con **pgAdmin**
(host = IP pública, puerto 5432, base `teaspeak`, usuario `teaspeak`, contraseña de `config.yml`, SSL `prefer`).

- Omitir el firewall en la instalación: `APPLY_FIREWALL=0 bash install.sh`.
- **Aviso:** SSH solo se admite desde `WHITELIST_SSH`; instala desde una IP de esa lista para no quedarte fuera.

## La `protocol_key`

Es la identidad del servidor y es **privada**: no está en este repositorio. Apórtala al instalar con
`PROTOCOL_KEY_B64` (contenido en base64) o cópiala manualmente a `/opt/teaspeak/protocol_key.txt` (`chmod 600`).

## Migrar desde la versión anterior (SQLite → PostgreSQL)

Si vienes de una instalación TeaSpeak anterior basada en **SQLite**, puedes traer todos tus datos a esta versión
PostgreSQL: servidores virtuales, canales, grupos, clientes, permisos, bans, tokens, cuentas ServerQuery y los
ficheros (**iconos**, avatares, conversaciones).

**Requisito:** un backup `.tar.gz` de tu versión anterior que contenga `TeaData.sqlite` y la carpeta `files/`
(es el formato que genera `backup.sh`).

```bash
# 1) Instala primero esta versión (crea el esquema PostgreSQL vacío)
curl -fsSL https://raw.githubusercontent.com/acrin96/teaspeak_v2/main/install.sh | bash

# 2) Sube tu backup a la VPS, p.ej. /root/teaspeak_backup.tar.gz

# 3) Descarga y ejecuta la migración
curl -fsSLO https://raw.githubusercontent.com/acrin96/teaspeak_v2/main/migration/migrate.sh
sudo bash migrate.sh /root/teaspeak_backup.tar.gz
```

El script para el servidor, ajusta los tipos del esquema (`TEXT`/`BIGINT`, porque SQLite no impone longitudes),
importa todas las tablas, copia `files/` (iconos incluidos), reajusta las secuencias de IDs, reinicia y muestra
un resumen con los recuentos migrados.

- **Reemplaza** los datos por defecto de la instalación limpia por los de tu backup, y **conserva** el
  `config.yml` nuevo (PostgreSQL + claves nuevas). Las contraseñas de `serveradmin`/ServerQuery pasan a ser las
  de tu producción anterior.
- Tras migrar, tus servidores virtuales usan sus puertos de producción: asegúrate de que el rango de voz del
  firewall (`UDP_PORTS` en `firewall.sh`) los cubre, o ajústalo.
- Motor de migración: `migration/migrate.sh` + `migration/migrate.py` (el `.sh` descarga el `.py` si falta).

## Manual

Manual completo de instalación y uso: ver la sección *Releases* / el enlace del manual.

## Notas

- Plataforma soportada: **Debian 11** (bullseye), amd64.
- La corrección anti-crash del handshake (validación DER) va **compilada en el binario**; ya no hace falta el
  antiguo script de mitigación por iptables.
- El código fuente con todos los parches se mantiene en un repositorio privado aparte.
