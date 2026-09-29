#!/opt/tsbot-dash/venv/bin/python
"""Mantenimiento de TeaSpeak: BUILD 2.1 (chat privado con el bot), en UN solo reinicio.

APROBADO por el dueno. PROGRAMADO para el 30-sep-2026 con cron de SISTEMA one-shot
(/etc/cron.d/ts-maint-build21-once, 09:57 CEST: aviso; corte a las 10:02). El resumen final por WhatsApp lo
manda post_build21.py (cron one-shot 10:20), que tambien fusiona las ramas y publica la release si todo fue OK.

Cambio (nada mas):
  Binario Build 2.1 (md5 11e755ad) sobre Build 2 (md5 ae2a571c): T06b, commit 2e02f24 de teaspeak_v2-src
  (rama build21-chat-bot). clientgetids / clientgetuidfromclid vuelven a resolver a los clientes query (el bot)
  y a los companeros de chat privado abierto, asi que el chat con el bot ya no se cierra con "Chat partner
  disconnected out of view". SIN cambios de config.yml ni de hilos (el config.yml se respalda igualmente).

Rutina acordada: poke a todos los conectados (SPY excluido; nick "TsBot Alert") ~5 min antes + 5 WhatsApp
de progreso SOLO al admin (admin_wa), backup (binario, config.yml, pg_dump), parar, cambiar binario,
arrancar, health-check, reinicio del bot y verificacion. ROLLBACK AUTOMATICO (binario Build 2 + config.yml
del backup) si falla el arranque, el health-check o la estabilidad. Guarda "touched": si falla antes de parar
TeaSpeak no se revierte ni se reinicia nada.

Parada del binario VIEJO (Build 2): la parada con SIGTERM de 1.4.21 aborta al final (SIGABRT al destruir los
IOEventLoop de VoiceIOManager, bug de upstream; ver docs/BUILD21.md) y deja un crash dump + "The server
crashed!" en el log ANTERIOR. Eso se INFORMA (WhatsApp, estado) pero NO cuenta como fallo si el binario nuevo
arranca sano. Los crashes del log NUEVO y los dumps posteriores al arranque si cuentan.

Post-checks (leidos de /opt/teaspeak/logs, del log de PostgreSQL y de /proc; no de journalctl):
  vservers online, errores conocidos en PG, crash en el log nuevo, crash dumps nuevos desde el arranque,
  ausencia de "failed to set synchronous_commit" / "ignoring invalid synchronous_commit" en el log nuevo (T15
  solo escribe una linea si falla), hilos del proceso (informativo), bots reconectados.
  Informativo: duracion de la parada del viejo, dumps y lineas de crash de esa parada.

Estado para el resumen final: /root/window-build21/state_build21.json (solo en modo real).

Modos:
  --check : valida hash, ldd, B2 en vivo, pg_dump de prueba, health, bots y credenciales de WhatsApp
            SIN reiniciar y SIN WhatsApp.
  (sin args): mantenimiento real (lo lanza el cron de sistema; ver docs/VENTANA_20260930.md en TsBot-Deploy).
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, "/opt/teaspeak/scripts")
import ts_maint as base  # wa_send, ts_healthy, count_clients, count_clients_expected, warn_poke, psql, _gc, ts_ops

CHECK = "--check" in sys.argv
base.CHECK = CHECK
LIVE = "/opt/teaspeak/TeaSpeakServer"
CONFIG = "/opt/teaspeak/config.yml"
NEW = "/root/build-out/build21/TeaSpeakServer.build21"
NEW_MD5 = "11e755ad0d84e644e151b056add0a58d"        # Build 2.1 = Build 2 + T06b (2e02f24, 29-sep 18:59)
EXPECTED_LIVE = "ae2a571c74fa997f84d215104af9f169"  # Build 2: debe estar ya en vivo
BACKUP_DIR = "/opt/teaspeak/backups"
PG_LOG = "/var/log/postgresql/postgresql-13-main.log"
TS_LOGS = "/opt/teaspeak/logs"
CRASH_DIR = "/opt/teaspeak/crash_dumps"
STATE = "/root/window-build21/state_build21.json"
TS_SERVICE, BOT_SERVICE = "teaspeak", "tsbot"
WARN_SECONDS = int(os.environ.get("WARN_SECONDS", "300"))
EXPECTED_VS = {int(x) for x in os.environ.get("EXPECTED_VS", "14").split(",") if x.strip()}
log, run, wa_send = base.log, base.run, base.wa_send
CRASH_PAT = "'Wrote crash dump|The server crashed|segfault|Assertion|terminate called'"
SYNC_FAIL_PAT = "'failed to set synchronous_commit|ignoring invalid synchronous_commit'"


# ───────────────────────── estado (para post_build21.py) ─────────────────────────
STATE_DATA = {"steps": {}}


def _save_state():
    if CHECK:
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE_DATA, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def st(step, result, **info):
    STATE_DATA["steps"][step] = {"result": result, "t": datetime.now().strftime("%H:%M:%S"), **info}
    _save_state()


def outcome(o, reason=""):
    STATE_DATA["outcome"], STATE_DATA["reason"] = o, reason
    STATE_DATA["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_state()


# ───────────────────────── utilidades ─────────────────────────
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


def ts_threads():
    """Hilos del proceso TeaSpeakServer (MainPID del servicio); -1 si no se puede leer."""
    try:
        pid = int(run(f"systemctl show -p MainPID --value {TS_SERVICE}").stdout.strip() or 0)
        return len(os.listdir(f"/proc/{pid}/task")) if pid > 0 else -1
    except (ValueError, OSError):
        return -1


async def ts_threads_stable(samples=3, gap=5):
    """Minimo de varias muestras (los 'Flush thread' de las desconexiones son transitorios)."""
    vals = []
    for i in range(samples):
        vals.append(ts_threads())
        if i < samples - 1:
            await asyncio.sleep(gap)
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else -1


# ───────────────────────── bots ─────────────────────────
def bot_targets():
    rows = base.psql("SELECT ts_server_id||'|'||ts_nickname FROM instances WHERE enabled ORDER BY id")
    out = []
    for r in rows.splitlines():
        sid, _, nick = r.partition("|")
        if sid.strip().isdigit():
            out.append((int(sid), nick))
    return out


async def bots_reconnected(timeout=120, min_wait=0):
    """(reconectados, total): instancias habilitadas con su cliente query (nick = ts_nickname) en su vserver."""
    targets = bot_targets()
    t0 = time.time()
    n = -1
    while True:
        try:
            pres = await base.ts_ops.bot_presence(base._ts_creds(), targets)
            n = sum(1 for sid, _ in targets if pres.get(int(sid), {}).get("bot"))
        except Exception as ex:  # noqa: BLE001
            log(f"[bots] error consultando presencia: {str(ex)[:120]}")
        if (n == len(targets) and time.time() - t0 >= min_wait) or time.time() - t0 >= timeout:
            return n, len(targets)
        await asyncio.sleep(10)


# ───────────────────────── flujo ─────────────────────────
async def main():
    log(f"=== ts_maint_build21 {'(CHECK)' if CHECK else '(REAL)'} ===")
    if not CHECK:
        STATE_DATA.update(started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), outcome="en_curso")
        _save_state()
    problems = []

    # 1) binario nuevo
    if not os.path.exists(NEW) or md5(NEW) != NEW_MD5:
        problems.append("binario Build 2.1 ausente o con hash inesperado")
    else:
        r = bash(f"LD_LIBRARY_PATH=/opt/teaspeak/libs ldd {NEW} | grep -c 'not found' || true")
        if r.stdout.strip() not in ("0", ""):
            problems.append(f"al binario Build 2.1 le faltan librerias ({r.stdout.strip()})")

    # 2) guarda: en vivo tiene que estar Build 2 (si ya esta Build 2.1, no se hace nada)
    live = md5(LIVE)
    if live == NEW_MD5:
        problems.append("Build 2.1 (11e755ad) YA esta en vivo: no hay nada que cambiar")
    elif live != EXPECTED_LIVE:
        problems.append(f"el binario vivo es {live[:8]}, no Build 2 ({EXPECTED_LIVE[:8]}): Build 2.1 va DESPUES de Build 2")

    # 3) config.yml: no se toca; solo que exista y sea YAML valido (se respalda y se restaura en el rollback)
    try:
        import yaml
        with open(CONFIG, encoding="utf-8") as f:
            yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        problems.append(f"config.yml ilegible o YAML invalido: {str(e).splitlines()[0][:120]}")

    threads_now = ts_threads()
    expected, clients = await base.count_clients_expected()
    log(f"vservers={expected} (esperados {sorted(EXPECTED_VS)}) clientes={clients} live_md5={live[:8]} hilos_ahora={threads_now}")
    if expected not in EXPECTED_VS:
        problems.append(f"hay {expected} vservers corriendo, se esperan {sorted(EXPECTED_VS)}")

    if CHECK:
        ok, info = await base.ts_healthy(expected, timeout=20)
        log(f"[check] health actual ok={ok} ({info})")
        if not ok:
            problems.append(f"health actual: {info}")
        n, tot = await bots_reconnected(timeout=0)
        log(f"[check] bots conectados ahora: {n}/{tot} (en real se exige {tot}/{tot} tras el reinicio del bot)")
        creds = (bool(base._gc('whatsapp_instance')), bool(base._gc('whatsapp_token')), bool(base._gc('admin_wa')))
        log("[check] WA creds presentes: inst=%s tok=%s to=%s" % creds)
        if not all(creds):
            problems.append("faltan credenciales de WhatsApp")
        os.makedirs("/root/work", exist_ok=True)
        tmp = f"/root/work/ts_maint_build21_check_{os.getpid()}.sql.gz"
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
        new_log, _ = ts_run_logs()
        log(f"[check] log actual {new_log}: crash={grep_count(new_log, CRASH_PAT)} "
            f"sync_commit_fallos={grep_count(new_log, SYNC_FAIL_PAT)} (informativo)")
        log("[check] SIN reinicio, SIN tocar binario/config.yml y SIN WhatsApp.")
        if problems:
            for p in problems:
                log(f"[check] FALLO: {p}")
            log("[check] NO ejecutar.")
            return 2
        log("[check] OK. No se toco produccion.")
        return 0

    # ── REAL ──
    st("preflight", "ok" if not problems else "fallo", live_md5=live, vservers=expected, clients=clients,
       threads_before_poke=threads_now, problems=problems)
    if problems:
        msg = "; ".join(problems)[:300]
        log(f"ABORTADO sin tocar nada: {msg}")
        wa_send(f"🚨 [Mantenimiento Build 2.1] Abortado ANTES de empezar (no se toco nada):\n\n{msg}")
        outcome("abortado_preflight", msg)
        log("=== ABORTADO SIN TOCAR NADA (preflight) ===")
        return 2

    ok_wa = wa_send("🛠️ [Aviso] Mantenimiento de TeaSpeak en ~5 min. Aviso por poke a los conectados.\n\n"
                    "Build 2.1: arreglo del chat privado con el bot (se cerraba a los 1-5 min con "
                    "'Chat partner disconnected out of view'). Sin cambios de configuracion.\n\n"
                    "Corte de voz ~1-2 min; todos reconectan solos.")
    poked = await base.warn_poke("[b][color=red]Maintenance in 5 min: ~2 min downtime, you will reconnect automatically.[/color][/b]")
    log(f"pokeados: {poked}")
    st("poke", "ok" if poked >= 0 else "fallo", poked=poked, clients_online=clients, wa_aviso=ok_wa)
    await asyncio.sleep(WARN_SECONDS)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_bin = f"{LIVE}.bak_{stamp}"
    bak_cfg = f"{CONFIG}.bak_{stamp}"
    touched = False
    cfg_stat = os.stat(CONFIG)
    since, since_epoch, start_epoch = None, time.time(), None
    try:
        wa_send(f"🔧 [1/5] Iniciando. Backup del binario + config.yml + dump de la BD teaspeak (poked={poked})...")
        shutil.copy2(LIVE, bak_bin)
        shutil.copy2(CONFIG, bak_cfg)
        os.chmod(bak_cfg, 0o600)
        if md5(bak_bin) != EXPECTED_LIVE:
            raise RuntimeError("el backup del binario no es Build 2")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dump = f"{BACKUP_DIR}/teaspeak_premaint_{stamp}.sql.gz"
        ok_dump, err = pg_dump_to(dump)
        if not ok_dump:
            raise RuntimeError(f"pg_dump fallo antes de tocar nada: {err}")
        log(f"backup bin -> {bak_bin} ; config -> {bak_cfg} ; dump -> {dump}")
        st("backups", "ok", bin=bak_bin, cfg=bak_cfg, dump=dump, dump_mb=round(os.path.getsize(dump) / 1048576, 1))

        wa_send("🔄 [2/5] Parando TeaSpeak y cambiando el binario (Build 2 -> Build 2.1)...")
        threads_before = ts_threads()
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
        st("swap", "ok", from_md5=EXPECTED_LIVE, to_md5=NEW_MD5, stop_secs=stop_secs, stop_dumps=stop_dumps)
        start_epoch = time.time()
        run(f"systemctl start {TS_SERVICE}")
        log(f"teaspeak arrancado con Build 2.1 (parada de B2: {stop_secs}s, dumps={stop_dumps}); verificando salud...")

        ok, info = await base.ts_healthy(expected)
        if not ok:
            raise RuntimeError(f"health-check fallo tras el cambio: {info}")
        await asyncio.sleep(10)
        if not service_active():
            raise RuntimeError("TeaSpeak se cayo a los pocos segundos de arrancar")
        st("teaspeak_up", "ok", info=info, expected=expected)

        wa_send(f"🟢 [3/5] TeaSpeak ARRIBA y estable ({info}). Reinicio el bot...")
        run(f"systemctl restart {BOT_SERVICE}")
        await asyncio.sleep(8)
        bot_active = run(f"systemctl is-active {BOT_SERVICE}").stdout.strip() == "active"
        wa_send("✅ [4/5] Bot reiniciado, instancias reconectando...")
        n_bots, tot_bots = await bots_reconnected(timeout=120, min_wait=30)
        log(f"bot activo={bot_active} ; instancias reconectadas {n_bots}/{tot_bots}")
        st("bot", "ok" if bot_active and n_bots == tot_bots else "revisar", active=bot_active,
           reconnected=n_bots, total=tot_bots)

        clients, vs = await base.count_clients()
        new_log, old_log = ts_run_logs()
        threads_after = await ts_threads_stable()
        res = {
            "vservers": vs,
            "pg_log_errors": pg_log_errors(since),
            "ts_log_crashes_new": grep_count(new_log, CRASH_PAT),
            "crash_dumps_new": crash_dumps_since(start_epoch),
            "sync_commit_failed_lines": grep_count(new_log, SYNC_FAIL_PAT),
            "threads_before": threads_before,
            "threads_after": threads_after,
            "bots": f"{n_bots}/{tot_bots}",
        }
        # parada del binario VIEJO (Build 2): solo informativo si el nuevo arranco sano (ver docstring)
        info_stop = {
            "b2_stop_secs": stop_secs,
            "b2_stop_crash_dumps": stop_dumps,
            "b2_stop_crash_in_log": grep_count(old_log, CRASH_PAT),
        }
        want = {"vservers": expected, "pg_log_errors": 0, "ts_log_crashes_new": 0, "crash_dumps_new": 0,
                "sync_commit_failed_lines": 0}
        bad = [k for k in want if res[k] != want[k]]
        if threads_after <= 0:
            bad.append("threads_after")
        if not bot_active or n_bots != tot_bots:
            bad.append("bots")
        log(f"post-checks: {res} ; parada B2 (informativo): {info_stop} ; malos={bad} ; "
            f"logs: nuevo={new_log} anterior={old_log}")
        st("postchecks", "ok" if not bad else "revisar", res=res, stop_info=info_stop, bad=bad, clients=clients)
        flag = "⚠️ REVISAR: " + ", ".join(bad) + ".\n\n" if bad else ""
        stop_note = (f"Parada de B2: {stop_secs}s, dumps={stop_dumps}, crash_log={info_stop['b2_stop_crash_in_log']} "
                     "(informativo: bug de parada conocido de 1.4.21, no del Build 2.1).")
        wa_send(f"🎉 [5/5] Mantenimiento Build 2.1 COMPLETADO.\n\n{flag}"
                f"{clients} clientes online ({vs}/{expected} vservers). Bots {n_bots}/{tot_bots}. "
                f"Hilos {threads_before} -> {threads_after}.\n\n"
                f"pg_err={res['pg_log_errors']} crash_nuevos={res['ts_log_crashes_new']}/{res['crash_dumps_new']} "
                f"sync_commit_fallos={res['sync_commit_failed_lines']}.\n\n{stop_note}")
        outcome("ok" if not bad else "avisos", ", ".join(bad))
        log(f"=== COMPLETADO {'CON AVISOS' if bad else 'OK'} ===")
        return 0 if not bad else 4

    except Exception as ex:
        reason = str(ex)[:200]
        log(f"!!! FALLO: {reason} -> ROLLBACK")
        try:
            if not touched:
                wa_send(f"❌ [Mantenimiento Build 2.1] Abortado ANTES de tocar TeaSpeak: {reason}.\n\n"
                        "No se reinicio nada; TeaSpeak y bot siguen igual.")
                outcome("abortado_sin_tocar", reason)
                log("=== ABORTADO SIN TOCAR NADA ===")
                return 1
            run(f"systemctl stop {TS_SERVICE}")
            shutil.copy2(bak_bin, LIVE)
            run(f"chown teaspeak:teaspeak {LIVE} && chmod 755 {LIVE}")
            shutil.copy2(bak_cfg, CONFIG)
            os.chown(CONFIG, cfg_stat.st_uid, cfg_stat.st_gid)
            os.chmod(CONFIG, cfg_stat.st_mode & 0o777)
            restored = md5(LIVE) == EXPECTED_LIVE and md5(CONFIG) == md5(bak_cfg)
            run(f"systemctl start {TS_SERVICE}")
            ok, info = await base.ts_healthy(expected)
            if ok and restored:
                run(f"systemctl restart {BOT_SERVICE}")
                wa_send(f"❌ [Mantenimiento Build 2.1] Fallo: {reason}.\n\nREVERTI al binario Build 2 y al config.yml "
                        f"del backup. TeaSpeak ARRIBA y estable ({info}). Bot reiniciado.")
                outcome("rollback_ok", f"{reason} | tras rollback: {info}")
                log("=== ROLLBACK OK ===")
                return 1
            wa_send(f"🚨 [Mantenimiento Build 2.1] CRITICO: fallo el cambio Y el rollback ({info}; restaurado={restored}).\n\n"
                    "INTERVENCION MANUAL YA (docs/VENTANA_20260930.md en TsBot-Deploy, rollback manual).")
            outcome("rollback_fallido", f"{reason} | rollback: {info}; restaurado={restored}")
            log("=== ROLLBACK FALLIDO ===")
            return 3
        except Exception as ex2:
            wa_send(f"🚨 [Mantenimiento Build 2.1] CRITICO: excepcion en el rollback ({str(ex2)[:150]}). INTERVENCION MANUAL YA.")
            try:
                outcome("rollback_fallido", f"{reason} | excepcion en rollback: {str(ex2)[:150]}")
            except Exception:  # noqa: BLE001
                pass
            log(f"=== ROLLBACK EXCEPCION: {ex2} ===")
            return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
