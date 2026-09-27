# Paquete de despliegue — F12 (rama `mejoras-semana1`)

**Maquina:** prod (23.26.121.40), `/opt/teaspeak/scripts`. **Ningun reinicio**: son scripts one-shot
que solo corren cuando se agenda un mantenimiento. **Aplicar DESPUES** del mantenimiento de binario
del 28-09 a las 10:02 (`/etc/cron.d/ts-maint-bin-once` ejecuta el `ts_maint_bin.py` desplegado a las
09:57; no tocar esos ficheros antes de que termine).

## Que cambia (auditoria F12)

| Antes | Ahora |
|---|---|
| `ts_maint.py` / `ts_maint_bin.py` con shebang `/opt/tsbot-dash/venv/bin/python` e `import ts_ops` de `/opt/tsbot-dash`: borrar el dashboard rompia el mantenimiento. | Shebang `/usr/bin/python3` (Debian, 3.9) + `scripts/ts_query.py` (cliente ServerQuery propio, solo stdlib). Sin venv: no hay dependencias de terceros, un venv vacio no aporta nada. |
| `count_clients_expected()` asumia **14** vservers si el conteo fallaba → rollback falso (si hay menos) o health-check ciego (si hay mas). | Devuelve `None` y ambos scripts **abortan sin tocar nada** (WhatsApp al admin, rc 2). |
| `ts_maint.py`: `pg_dump | gzip` sin comprobar (dash, sin pipefail). | `pg_dump_to()` con `pipefail` + tamaño minimo; si falla, aborta sin tocar TeaSpeak (flag `touched`, igual que `ts_maint_bin.py`: un fallo antes de cambiar nada ya no reinicia TeaSpeak "por rollback"). |

Probado en prod el 28-09 (copia en `/tmp`, `/usr/bin/python3` 3.9.2): `py_compile` OK;
`ts_maint.py --check` → `health actual: ok=True (14 vservers online)`, rc 0; simulacion de conteo
fallido → `ABORTADO sin tocar nada`, rc 2.

Ademas (deriva Q01): el `scripts/sysctl_tuning.sh` de `/opt/teaspeak/scripts` es la version vieja
(`wmem_default` 2 MB); el del repo (8 MB) es el que esta aplicado en vivo
(`/etc/sysctl.d/99-teaspeak-voice.conf`, `sysctl net.core.wmem_default` = 8388608). Se copia el del
repo **sin re-aplicarlo** (no cambia nada en el kernel).

## Despliegue (prod, como root, tras el mantenimiento de las 10:02)

```bash
set -e
[ -e /etc/cron.d/ts-maint-bin-once ] && { echo "ts-maint-bin-once aun agendado: esperar"; exit 1; }
cd /root/work/teaspeak_v2 && git fetch -q origin
TS=$(date +%Y%m%d_%H%M%S); D=/opt/teaspeak/scripts
for f in ts_maint.py ts_maint_bin.py sysctl_tuning.sh; do cp -p $D/$f $D/$f.bak_$TS; done
for f in ts_query.py ts_maint.py ts_maint_bin.py; do
  git show origin/mejoras-semana1:scripts/$f > $D/$f
done
git show origin/mejoras-semana1:scripts/sysctl_tuning.sh > $D/sysctl_tuning.sh
chown root:root $D/ts_query.py $D/ts_maint.py $D/ts_maint_bin.py
chmod 755 $D/ts_maint.py $D/sysctl_tuning.sh; chmod 750 $D/ts_maint_bin.py; chmod 644 $D/ts_query.py
```

## Verificacion

```bash
/usr/bin/python3 -m py_compile /opt/teaspeak/scripts/{ts_query,ts_maint,ts_maint_bin}.py && echo OK
grep -l 'tsbot-dash' /opt/teaspeak/scripts/*.py || echo "sin dependencia de tsbot-dash"
/opt/teaspeak/scripts/ts_maint.py --check        # solo lectura; "health actual: ok=True (N vservers online)"
```
(`ts_maint_bin.py --check` tambien funciona, pero valida el md5 de un build concreto y hace un
`pg_dump` de prueba: solo tiene sentido cuando se prepare el siguiente cambio de binario.)

## Rollback

```bash
D=/opt/teaspeak/scripts
for f in ts_maint.py ts_maint_bin.py sysctl_tuning.sh; do cp -p $D/$f.bak_$TS $D/$f; done
rm -f $D/ts_query.py
```
(El rollback vuelve a depender de `/opt/tsbot-dash`, que sigue existiendo.)

## Pagina de pago

Su parte de F12 (venv y `.env` propios de `tsbot-pay`) esta en `TsBot-Deploy/docs/DEPLOY_SEMANA1.md`
(rama `mejoras-semana1`): el codigo de la pagina vive en `/opt/tsbot-pay` y no pertenece a este repo.
