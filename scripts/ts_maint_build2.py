#!/opt/tsbot-dash/venv/bin/python
"""Mantenimiento de TeaSpeak: BUILD 2 (rendimiento T13-T17 + T11 + T21/T22 crashes en desconexiones/rechazos masivos). PREPARADO, NO PROGRAMADO.

Solo binario (sin cambios de config ni de contrasenas). Requiere que Build 1 (md5 ef2c6095) este en vivo y
validado: Build 2 = Build 1 + los 8 commits de codigo de la rama build2-rendimiento (acrin96/teaspeak_v2-src).

Rutina acordada: poke a todos los conectados (SPY excluido) ~5 min antes + 5 WhatsApp de progreso al ADMIN
(admin_wa), backup (binario + pg_dump), parar, cambiar, arrancar, health-check, reinicio del bot y verificacion.
ROLLBACK AUTOMATICO (solo binario) si falla el arranque, el health-check o la estabilidad.
Guarda "touched": si falla antes de parar TeaSpeak no se revierte ni se reinicia nada.

Es la primera parada de un binario con el fix T01 (Build 1): el post-check mira tambien que la PARADA de
Build 1 haya sido limpia (sin crash dump ni "The server crashed" en el log anterior) = V10 en produccion.

Post-checks leidos de /opt/teaspeak/logs y del log de PostgreSQL (no de journalctl):
  vservers online, errores conocidos en PG, crash en el log nuevo, crash dumps nuevos, parada limpia de B1,
  linea "synchronous_commit: off" del arranque (T15).

Modos:
  --check : valida hash, ldd, B1 en vivo, pg_dump de prueba, health y WhatsApp SIN reiniciar ni avisar.
  (sin args): mantenimiento real. Lanzarlo con systemd-run a las 09:57 (ver docs/BUILD2.md); SIN cron.
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, "/opt/teaspeak/scripts")
import ts_maint as base  # wa_send, ts_healthy, count_clients, count_clients_expected, warn_poke, psql, _gc

CHECK = "--check" in sys.argv
base.CHECK = CHECK
LIVE = "/opt/teaspeak/TeaSpeakServer"
NEW = "/root/build-out/build2/TeaSpeakServer.build2"
NEW_MD5 = "ae2a571c74fa997f84d215104af9f169"        # Build 2 + T21 + T22 (fe4bac7, 28-sep 18:35); sha256 b40d9872...
EXPECTED_LIVE = "ef2c609533996e81044b33c7e3c09d71"  # Build 1 + T20c: debe estar ya en vivo
BACKUP_DIR = "/opt/teaspeak/backups"
PG_LOG = "/var/log/postgresql/postgresql-13-main.log"
TS_LOGS = "/opt/teaspeak/logs"
CRASH_DIR = "/opt/teaspeak/crash_dumps"
TS_SERVICE, BOT_SERVICE = "teaspeak", "tsbot"
WARN_SECONDS = int(os.environ.get("WARN_SECONDS", "300"))
EXPECTED_VS = int(os.environ.get("EXPECTED_VS", "14"))
log, run, wa_send = base.log, base.run, base.wa_send
CRASH_PAT = "'Wrote crash dump|The server crashed|segfault|Assertion|terminate called'"


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bash(cmd, **kw):
    return subprocess.run(["bash", "-c", "set -o pipefail; " + cmd], capture_output=True, text=True, cwd="/tmp", **kw)


def pg_dump_to(path):
    r = bash(f"sudo -u postgres pg_dump --no-owner --no-privileges teaspeak | gzip > {path}")
    ok = r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 1024
    if os.path.exists(path):
        os.chmod(path, 0o600)
    return ok, (r.stderr or "")[:160]


def _count(r):
    try:
        return int((r.stdout or "").strip().splitlines()[-1] or 0)
    except (ValueError, IndexError):
        return -1


def pg_log_errors(since):
    return _count(bash(f"awk -v s='{since}' 'substr($0,1,19) >= s' {PG_LOG} | grep -ciE "
                       "'groups_pkey|LIMIT must not be negative|more expressions than target|at or near .FORM.|"
                       "pk_properties|synchronous_commit|teaspeak@teaspeak ERROR' || true"))


def ts_run_logs():
    logs = sorted(glob.glob(f"{TS_LOGS}/*_general.log"), key=os.path.getmtime)
    return (logs[-1] if logs else None), (logs[-2] if len(logs) > 1 else None)


def grep_count(path, pattern):
    if not path:
        return -1
    return _count(bash(f"grep -ciE {pattern} '{path}' || true"))


def crash_dumps_since(epoch):
    return sum(1 for f in glob.glob(f"{CRASH_DIR}/*.dmp") if os.path.getmtime(f) >= epoch)


def service_active():
    return run(f"systemctl is-active {TS_SERVICE}").stdout.strip() == "active"


async def main():
    log(f"=== ts_maint_build2 {'(CHECK)' if CHECK else '(REAL)'} ===")
    problems = []
    if not os.path.exists(NEW) or md5(NEW) != NEW_MD5:
        problems.append("binario Build 2 ausente o con hash inesperado")
    else:
        r = bash(f"LD_LIBRARY_PATH=/opt/teaspeak/libs ldd {NEW} | grep -c 'not found' || true")
        if r.stdout.strip() not in ("0", ""):
            problems.append(f"al binario Build 2 le faltan librerias ({r.stdout.strip()})")
    live = md5(LIVE)
    if live != EXPECTED_LIVE:
        problems.append(f"el binario vivo es {live[:8]}, no Build 1 ({EXPECTED_LIVE[:8]}): Build 2 va DESPUES de Build 1")
    expected, clients = await base.count_clients_expected()
    log(f"vservers={expected} (esperados {EXPECTED_VS}) clientes={clients} live_md5={live[:8]}")
    if expected != EXPECTED_VS:
        problems.append(f"hay {expected} vservers corriendo, se esperan {EXPECTED_VS}")

    if CHECK:
        ok, info = await base.ts_healthy(expected, timeout=20)
        log(f"[check] health actual ok={ok} ({info})")
        if not ok:
            problems.append(f"health actual: {info}")
        creds = (bool(base._gc('whatsapp_instance')), bool(base._gc('whatsapp_token')), bool(base._gc('admin_wa')))
        log("[check] WA creds presentes: inst=%s tok=%s to=%s" % creds)
        if not all(creds):
            problems.append("faltan credenciales de WhatsApp")
        tmp = f"/tmp/ts_maint_build2_check_{os.getpid()}.sql.gz"
        okd, err = pg_dump_to(tmp)
        size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if os.path.exists(tmp):
            os.remove(tmp)
        log(f"[check] pg_dump de prueba ok={okd} size={size} {err}")
        if not okd:
            problems.append("pg_dump de prueba")
        free = shutil.disk_usage(BACKUP_DIR).free // (1 << 20)
        log(f"[check] espacio libre en {BACKUP_DIR}: {free} MiB")
        if free < 2048:
            problems.append("menos de 2 GiB libres para backups")
        log("[check] SIN reinicio y SIN WhatsApp.")
        if problems:
            for p in problems:
                log(f"[check] FALLO: {p}")
            log("[check] NO ejecutar.")
            return 2
        log("[check] OK. No se toco produccion.")
        return 0

    if problems:
        msg = "; ".join(problems)[:300]
        log(f"ABORTADO sin tocar nada: {msg}")
        wa_send(f"🚨 [Mantenimiento Build 2] Abortado ANTES de empezar (no se toco nada): {msg}")
        return 2

    wa_send("🛠️ [Aviso] Mantenimiento de TeaSpeak en ~5 min. Aviso por poke a los conectados. "
            "Build 2: mejoras de rendimiento (menos lag del tick, ediciones de canal sin bloquear, escrituras a la BD "
            "mas rapidas) y arreglo de dos crashes con desconexiones masivas. Corte de voz ~1-2 min; todos reconectan solos.")
    poked = await base.warn_poke("[b][color=red]Maintenance in 5 min: ~2 min downtime, you will reconnect automatically.[/color][/b]")
    log(f"pokeados: {poked}")
    await asyncio.sleep(WARN_SECONDS)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_bin = f"{LIVE}.bak_{stamp}"
    touched = False
    since, since_epoch, start_epoch = None, time.time(), None
    try:
        wa_send(f"🔧 [1/5] Iniciando. Backup del binario + dump de la BD teaspeak (poked={poked})...")
        shutil.copy2(LIVE, bak_bin)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dump = f"{BACKUP_DIR}/teaspeak_premaint_{stamp}.sql.gz"
        ok_dump, err = pg_dump_to(dump)
        if not ok_dump:
            raise RuntimeError(f"pg_dump fallo antes de tocar nada: {err}")
        log(f"backup bin -> {bak_bin} ; dump -> {dump}")

        wa_send("🔄 [2/5] Parando TeaSpeak (primera parada con el fix T01) y cambiando el binario...")
        since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        since_epoch = time.time()
        touched = True
        t_stop = time.time()
        run(f"systemctl stop {TS_SERVICE}")
        stop_secs = round(time.time() - t_stop, 1)
        stop_dumps = crash_dumps_since(since_epoch)
        shutil.copy2(NEW, LIVE + ".tmp")
        run(f"chown teaspeak:teaspeak {LIVE}.tmp && chmod 755 {LIVE}.tmp && mv -f {LIVE}.tmp {LIVE}")
        if md5(LIVE) != NEW_MD5:
            raise RuntimeError("el binario copiado no tiene el md5 esperado")
        start_epoch = time.time()
        run(f"systemctl start {TS_SERVICE}")
        log(f"teaspeak arrancado con Build 2 (parada de B1: {stop_secs}s, dumps={stop_dumps}); verificando salud...")

        ok, info = await base.ts_healthy(expected)
        if not ok:
            raise RuntimeError(f"health-check fallo tras el cambio: {info}")
        await asyncio.sleep(10)
        if not service_active():
            raise RuntimeError("TeaSpeak se cayo a los pocos segundos de arrancar")

        wa_send(f"🟢 [3/5] TeaSpeak ARRIBA y estable ({info}). Reinicio el bot...")
        run(f"systemctl restart {BOT_SERVICE}")
        await asyncio.sleep(8)
        wa_send("✅ [4/5] Bot reiniciado, instancias reconectando...")
        await asyncio.sleep(40)

        clients, vs = await base.count_clients()
        new_log, old_log = ts_run_logs()
        res = {
            "vservers": vs,
            "pg_log_errors": pg_log_errors(since),
            "ts_log_crashes_new": grep_count(new_log, CRASH_PAT),
            "crash_dumps_new": crash_dumps_since(start_epoch),
            "b1_stop_crash_dumps": stop_dumps,
            "b1_stop_crash_in_log": grep_count(old_log, CRASH_PAT),
            "sync_commit_off_line": grep_count(new_log, "'synchronous_commit: off'"),
        }
        want = {"vservers": expected, "pg_log_errors": 0, "ts_log_crashes_new": 0, "crash_dumps_new": 0,
                "b1_stop_crash_dumps": 0, "b1_stop_crash_in_log": 0}
        bad = [k for k in want if res[k] != want[k]]
        if res["sync_commit_off_line"] < 1:
            bad.append("sync_commit_off_line")
        log(f"post-checks: {res} ; parada B1 {stop_secs}s ; malos={bad} ; logs: nuevo={new_log} anterior={old_log}")
        flag = "⚠️ REVISAR: " + ", ".join(bad) + ". " if bad else ""
        wa_send(f"🎉 [5/5] Mantenimiento Build 2 COMPLETADO. {flag}{clients} clientes online ({vs}/{expected} vservers). "
                f"pg_err={res['pg_log_errors']} crash_nuevos={res['ts_log_crashes_new']}/{res['crash_dumps_new']} "
                f"parada_B1={stop_secs}s dumps={stop_dumps} crash_log={res['b1_stop_crash_in_log']} "
                f"sync_commit_off={res['sync_commit_off_line']}.")
        log(f"=== COMPLETADO {'CON AVISOS' if bad else 'OK'} ===")
        return 0 if not bad else 4

    except Exception as ex:
        reason = str(ex)[:200]
        log(f"!!! FALLO: {reason} -> ROLLBACK")
        try:
            if not touched:
                wa_send(f"❌ [Mantenimiento Build 2] Abortado ANTES de tocar TeaSpeak: {reason}. "
                        "No se reinicio nada; TeaSpeak y bot siguen igual.")
                log("=== ABORTADO SIN TOCAR NADA ===")
                return 1
            run(f"systemctl stop {TS_SERVICE}")
            shutil.copy2(bak_bin, LIVE)
            run(f"chown teaspeak:teaspeak {LIVE} && chmod 755 {LIVE}")
            run(f"systemctl start {TS_SERVICE}")
            ok, info = await base.ts_healthy(expected)
            if ok:
                run(f"systemctl restart {BOT_SERVICE}")
                wa_send(f"❌ [Mantenimiento Build 2] Fallo: {reason}. REVERTI al binario Build 1. "
                        f"TeaSpeak ARRIBA y estable ({info}). Bot reiniciado.")
                log("=== ROLLBACK OK ===")
                return 1
            wa_send(f"🚨 [Mantenimiento Build 2] CRITICO: fallo el cambio Y el rollback ({info}). "
                    "INTERVENCION MANUAL YA (docs/BUILD2.md, rollback manual).")
            log("=== ROLLBACK FALLIDO ===")
            return 3
        except Exception as ex2:
            wa_send(f"🚨 [Mantenimiento Build 2] CRITICO: excepcion en el rollback ({str(ex2)[:150]}). INTERVENCION MANUAL YA.")
            log(f"=== ROLLBACK EXCEPCION: {ex2} ===")
            return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
