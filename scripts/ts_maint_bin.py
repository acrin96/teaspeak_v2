#!/opt/tsbot-dash/venv/bin/python
"""Mantenimiento auto-ejecutable de TeaSpeak: CAMBIO DE BINARIO.

Despliega /root/build-out/TeaSpeakServer.new (fix Postgres: upsert en properties -> sin
'duplicate key pk_properties'; letters.letterid como identity -> mensajes offline), reinicia,
verifica salud y HACE ROLLBACK AUTOMATICO al binario anterior si algo va mal, avisando al admin
por WhatsApp en cada paso (rutina acordada: poke previo a todos + 5 WhatsApp al admin).
One-shot (se auto-desprograma).

Modos:
  --check : valida binario nuevo, health-check y credenciales SIN tocar produccion ni enviar WhatsApp.
  (sin args): ejecuta el mantenimiento real.
"""
from __future__ import annotations
import asyncio, os, subprocess, sys, time, shutil, hashlib
from datetime import datetime

sys.path.insert(0, "/opt/teaspeak/scripts")
import ts_maint as base  # reutiliza wa_send, ts_healthy, count_clients, warn_poke, psql

CHECK = "--check" in sys.argv
base.CHECK = CHECK
CRON_FILE = "/etc/cron.d/ts-maint-bin-once"
LIVE = "/opt/teaspeak/TeaSpeakServer"
NEW = "/root/build-out/TeaSpeakServer.new"
NEW_SHA = "50d24fd12124fab9fb576b5c964f2420"  # md5 del build del 26-sep
BACKUP_DIR = "/opt/teaspeak/backups"
TS_SERVICE, BOT_SERVICE = "teaspeak", "tsbot"
WARN_SECONDS = int(os.environ.get("WARN_SECONDS", "300"))
log, run, wa_send = base.log, base.run, base.wa_send


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pg_dump_to(path):
    """pg_dump | gzip con pipefail en bash (el run() base usa /bin/sh = dash, sin pipefail)."""
    cmd = f"set -o pipefail; sudo -u postgres pg_dump --no-owner --no-privileges teaspeak | gzip > {path}"
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd="/tmp")
    ok = r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 1024
    return ok, (r.stderr or "")[:160]


def letterid_identity():
    return base.psql("SELECT is_identity FROM information_schema.columns WHERE table_name='letters' "
                     "AND column_name='letterid'", db="teaspeak")


def new_log_errors(since):
    r = run(f"journalctl -u {TS_SERVICE} --since '{since}' --no-pager -o cat | "
            "grep -ciE 'pk_properties|letterid|segfault|Assertion|terminate called'")
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return -1


async def main():
    if not CHECK and os.path.exists(CRON_FILE):
        run(f"rm -f {CRON_FILE}"); log("cron one-shot eliminado")
    log(f"=== ts_maint_bin {'(CHECK)' if CHECK else '(REAL)'} ===")

    if not os.path.exists(NEW) or md5(NEW) != NEW_SHA:
        msg = "binario nuevo ausente o con hash inesperado. ABORTADO sin tocar nada."
        log(msg); wa_send(f"🚨 [Mantenimiento] {msg}"); return 2
    r = run(f"LD_LIBRARY_PATH=/opt/teaspeak/libs ldd {NEW} | grep -c 'not found'")
    if r.stdout.strip() not in ("0", ""):
        msg = f"al binario nuevo le faltan librerias ({r.stdout.strip()}). ABORTADO sin tocar nada."
        log(msg); wa_send(f"🚨 [Mantenimiento] {msg}"); return 2
    expected, clients = await base.count_clients_expected()
    log(f"vservers esperados={expected} clientes={clients} letterid_identity={letterid_identity()}")

    if CHECK:
        ok, info = await base.ts_healthy(expected, timeout=20)
        log(f"[check] health actual ok={ok} ({info})")
        log("[check] WA creds presentes: inst=%s tok=%s to=%s" % (
            bool(base._gc('whatsapp_instance')), bool(base._gc('whatsapp_token')), bool(base._gc('admin_wa'))))
        log(f"[check] live md5={md5(LIVE)} new md5={md5(NEW)}")
        # ensaya el mismo camino de backup que usa el modo real (lo que fallo el 27-sep)
        tmp = f"/tmp/ts_maint_check_{os.getpid()}.sql.gz"
        ok, err = pg_dump_to(tmp)
        size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if os.path.exists(tmp):
            os.remove(tmp)
        log(f"[check] pg_dump de prueba ok={ok} size={size} {err}")
        if not ok:
            log("[check] FALLO el dump de prueba: NO agendar.")
            return 2
        free = shutil.disk_usage(BACKUP_DIR).free
        log(f"[check] espacio libre en {BACKUP_DIR}: {free // (1 << 20)} MiB")
        log("[check] OK. No se toco produccion.")
        return 0

    wa_send("🛠️ [Aviso] Mantenimiento de TeaSpeak en ~5 min (10:02). Aviso por poke a los conectados. "
            "Nuevo binario con correcciones de base de datos. Corte de voz ~1-2 min; todos reconectan solos.")
    poked = await base.warn_poke("[b][color=red]Maintenance in 5 min: ~2 min downtime, you will reconnect automatically.[/color][/b]")
    log(f"pokeados: {poked}")
    await asyncio.sleep(WARN_SECONDS)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{LIVE}.bak_{stamp}"
    touched = False  # solo se hace rollback (y reinicio) si ya se paro/cambio TeaSpeak
    try:
        wa_send(f"🔧 [1/5] Iniciando. Backup del binario/config + dump de la BD teaspeak (poked={poked})...")
        shutil.copy2(LIVE, bak)
        shutil.copy2("/opt/teaspeak/config.yml", f"/opt/teaspeak/config.yml.bak_{stamp}")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dump = f"{BACKUP_DIR}/teaspeak_premaint_{stamp}.sql.gz"
        ok_dump, err = pg_dump_to(dump)
        if not ok_dump:
            raise RuntimeError(f"pg_dump fallo antes de tocar nada: {err}")
        log(f"backup bin -> {bak} ; dump -> {dump}")

        wa_send("🔄 [2/5] Parando TeaSpeak y cambiando el binario...")
        since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        touched = True
        run(f"systemctl stop {TS_SERVICE}")
        shutil.copy2(NEW, LIVE + ".tmp")
        run(f"chown teaspeak:teaspeak {LIVE}.tmp && chmod 755 {LIVE}.tmp && mv -f {LIVE}.tmp {LIVE}")
        run(f"systemctl start {TS_SERVICE}")
        log("teaspeak arrancado con binario nuevo; verificando salud...")

        ok, info = await base.ts_healthy(expected)
        if not ok:
            raise RuntimeError(f"health-check fallo tras el cambio: {info}")
        await asyncio.sleep(10)
        if run(f"systemctl is-active {TS_SERVICE}").stdout.strip() != "active":
            raise RuntimeError("TeaSpeak se cayo a los pocos segundos de arrancar")

        wa_send(f"🟢 [3/5] TeaSpeak ARRIBA y estable ({info}). Reinicio el bot...")
        run(f"systemctl restart {BOT_SERVICE}")
        await asyncio.sleep(8)
        wa_send("✅ [4/5] Bot reiniciado, instancias reconectando...")
        await asyncio.sleep(40)
        clients, vs = await base.count_clients()
        ident = letterid_identity()
        errs = new_log_errors(since)
        wa_send(f"🎉 [5/5] Mantenimiento COMPLETADO. Binario nuevo activo. {clients} clientes online "
                f"({vs} vservers). letters.letterid identity={ident}; errores pk_properties/crash en log: {errs}.")
        log(f"=== COMPLETADO OK clients={clients} vs={vs} ident={ident} errs={errs} ===")
        return 0

    except Exception as ex:
        reason = str(ex)[:200]
        log(f"!!! FALLO: {reason} -> ROLLBACK")
        try:
            if not touched:
                wa_send(f"❌ [Mantenimiento] Abortado ANTES de tocar TeaSpeak: {reason}. "
                        "No se reinicio nada; TeaSpeak y bot siguen igual.")
                log("=== ABORTADO SIN TOCAR NADA ===")
                return 1
            run(f"systemctl stop {TS_SERVICE}")
            shutil.copy2(bak, LIVE)
            run(f"chown teaspeak:teaspeak {LIVE} && chmod 755 {LIVE}")
            run(f"systemctl start {TS_SERVICE}")
            ok, info = await base.ts_healthy(expected)
            if ok:
                run(f"systemctl restart {BOT_SERVICE}")
                wa_send(f"❌ [Mantenimiento] Fallo el cambio: {reason}. REVERTI al binario anterior. "
                        f"TeaSpeak ARRIBA y estable ({info}). Bot reiniciado.")
                log("=== ROLLBACK OK ===")
                return 1
            wa_send(f"🚨 [Mantenimiento] CRITICO: fallo el cambio Y el rollback ({info}). INTERVENCION MANUAL YA.")
            log("=== ROLLBACK FALLIDO ===")
            return 3
        except Exception as ex2:
            wa_send(f"🚨 [Mantenimiento] CRITICO: excepcion en el rollback ({str(ex2)[:150]}). INTERVENCION MANUAL YA.")
            log(f"=== ROLLBACK EXCEPCION: {ex2} ===")
            return 3


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
