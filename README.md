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

## Firewall y acceso a PostgreSQL (pgAdmin)

El instalador **aplica el firewall por defecto** (`scripts/firewall.sh`, whitelist en su cabecera): SSH,
ServerQuery y PostgreSQL quedan abiertos solo a tus IPs; voz y ficheros públicos con *rate-limit*. Si
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
