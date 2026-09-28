#!/opt/tsbot-dash/venv/bin/python
"""Mantenimiento de TeaSpeak: BUILD 1 (integridad de datos + seguridad) + B15 + clients.teaspeak: 0.

En UN solo reinicio:
  1. Binario Build 1 (/root/build-out/build1/TeaSpeakServer.build1, md5 2ea06c90...).
  2. B15: rota la contrasena del rol PostgreSQL `teaspeak` (ALTER ROLE justo antes del arranque) y la
     actualiza en /opt/teaspeak/config.yml (general.database.url y log.instance_logs_url).
     La contrasena se genera aqui (secrets, 32 alfanumericos) y NUNCA se imprime; a la BD se le pasa ya
     como verificador md5 (nunca en claro), por stdin (no aparece en `ps`) y con log_statement=none.
  3. server.clients.teaspeak: 0 en config.yml (desactiva el handshake del cliente TeaSpeak; T05).

Rutina acordada: poke a todos los conectados (SPY excluido) ~5 min antes + 5 WhatsApp de progreso al
ADMIN (admin_wa), backup (binario, config, verificador del rol y pg_dump), parar, cambiar, arrancar,
health-check, reinicio del bot y verificacion. ROLLBACK AUTOMATICO (binario + config + contrasena del
rol) si falla el arranque, el health-check, la estabilidad o el login a PG con la contrasena nueva.
Guarda "touched": si falla antes de parar TeaSpeak no se revierte ni se reinicia nada.

Post-checks leidos de /opt/teaspeak/logs y del log de PostgreSQL (no de journalctl):
  vservers online, secuencia de groups >= MAX(groupid), filas lock_test = 1, letters.letterid identity,
  patrones de error/crash nuevos, login de TeaSpeak a PG con la contrasena nueva, clave teaspeak en config.

Modos:
  --check : valida TODO (hash, ldd, base B1 en vivo, edicion de config + parseo YAML en seco, login a PG
            con la contrasena ACTUAL, lectura del verificador del rol, pg_dump de prueba, health, WhatsApp)
            SIN ALTER, SIN reinicio y SIN WhatsApp. Antes de B1 marca "PENDIENTE B1" y sale 0.
  (sin args): mantenimiento real. SIN cron: lo lanza el ejecutor (ver RUNBOOK.md) con systemd-run.
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, "/opt/teaspeak/scripts")
import ts_maint as base  # wa_send, ts_healthy, count_clients, count_clients_expected, warn_poke, psql, _gc

CHECK = "--check" in sys.argv
base.CHECK = CHECK
LIVE = "/opt/teaspeak/TeaSpeakServer"
NEW = "/root/build-out/build1/TeaSpeakServer.build1"
NEW_MD5 = "2ea06c90c9a4f1d4ed08efc59e786edd"
EXPECTED_LIVE = "50d24fd12124fab9fb576b5c964f2420"   # binario de B1 (e02aefb) que debe estar ya en vivo
CONFIG = "/opt/teaspeak/config.yml"
BACKUP_DIR = "/opt/teaspeak/backups"
PG_ROLE = "teaspeak"
PG_LOG = "/var/log/postgresql/postgresql-13-main.log"
TS_LOGS = "/opt/teaspeak/logs"
CRASH_DIR = "/opt/teaspeak/crash_dumps"
TS_SERVICE, BOT_SERVICE = "teaspeak", "tsbot"
WARN_SECONDS = int(os.environ.get("WARN_SECONDS", "300"))
EXPECTED_VS = int(os.environ.get("EXPECTED_VS", "14"))
PW_RE = re.compile(r"(postgres(?:ql)?://" + PG_ROLE + r":)([^@\s\"']+)(@)")
log, run, wa_send = base.log, base.run, base.wa_send


# ───────────────────────── utilidades ─────────────────────────
def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bash(cmd, **kw):
    """bash con pipefail (el run() base usa /bin/sh = dash, sin pipefail: fallo del 27-sep)."""
    return subprocess.run(["bash", "-c", "set -o pipefail; " + cmd], capture_output=True, text=True, cwd="/tmp", **kw)


def pg_dump_to(path):
    r = bash(f"sudo -u postgres pg_dump --no-owner --no-privileges teaspeak | gzip > {path}")
    ok = r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 1024
    if os.path.exists(path):
        os.chmod(path, 0o600)
    return ok, (r.stderr or "")[:160]


def psql_ts(sql):
    return base.psql(sql, db="teaspeak")


def letterid_identity():
    return psql_ts("SELECT is_identity FROM information_schema.columns WHERE table_name='letters' "
                   "AND column_name='letterid'")


def groups_seq_ok():
    return psql_ts("SELECT s.last_value >= (SELECT max(groupid) FROM groups) FROM pg_sequences s "
                   "WHERE s.schemaname||'.'||s.sequencename = pg_get_serial_sequence('groups','groupid')")


def lock_test_rows():
    return psql_ts("SELECT count(*) FROM general WHERE key='lock_test'")


def _count(r):
    try:
        return int((r.stdout or "").strip().splitlines()[-1] or 0)
    except (ValueError, IndexError):
        return -1


def pg_log_errors(since):
    """Errores conocidos en el log de PG desde `since` ('YYYY-MM-DD HH:MM:SS'; cada linea empieza por %m)."""
    return _count(bash(f"awk -v s='{since}' 'substr($0,1,19) >= s' {PG_LOG} | grep -ciE "
                       "'groups_pkey|LIMIT must not be negative|more expressions than target|at or near .FORM.|pk_properties' || true"))


def pg_auth_failures(since):
    return _count(bash(f"awk -v s='{since}' 'substr($0,1,19) >= s' {PG_LOG} | grep -ciE "
                       f"'password authentication failed for user .{PG_ROLE}.' || true"))


CRASH_PAT = "'Wrote crash dump|The server crashed|segfault|Assertion|terminate called'"


def ts_run_logs():
    """(log del arranque nuevo, log de la ejecucion anterior) = los 2 *_general.log mas recientes."""
    logs = sorted(glob.glob(f"{TS_LOGS}/*_general.log"), key=os.path.getmtime)
    return (logs[-1] if logs else None), (logs[-2] if len(logs) > 1 else None)


def ts_log_crashes(path):
    if not path:
        return -1
    return _count(bash(f"grep -ciE {CRASH_PAT} '{path}' || true"))


def crash_dumps_since(epoch):
    return sum(1 for f in glob.glob(f"{CRASH_DIR}/*.dmp") if os.path.getmtime(f) >= epoch)


def service_active():
    return run(f"systemctl is-active {TS_SERVICE}").stdout.strip() == "active"


# ───────────────────────── config.yml ─────────────────────────
def edit_config(src: str, new_pw: str):
    """Devuelve (texto_nuevo, cambios). Sustituye la contrasena del rol en TODAS las URLs postgres://teaspeak:...@
    (todas deben llevar la misma) y fija server.clients.teaspeak: 0 (insertandola si falta). Nunca devuelve ni
    registra contrasenas: `cambios` solo lleva descripciones."""
    changes = []
    olds = {m.group(2) for m in PW_RE.finditer(src)}
    n_urls = len(PW_RE.findall(src))
    if n_urls == 0:
        raise ValueError("no hay ninguna URL postgres://teaspeak:...@ en config.yml")
    if len(olds) != 1:
        raise ValueError(f"las {n_urls} URLs del rol {PG_ROLE} no llevan la misma contrasena")
    src2 = PW_RE.sub(lambda m: m.group(1) + new_pw + m.group(3), src)
    changes.append(f"contrasena del rol {PG_ROLE} sustituida en {n_urls} URL(s)")

    lines = src2.split("\n")
    out, in_server, in_clients, done, anchor = [], False, False, False, None
    for i, line in enumerate(lines):
        s = line.lstrip()
        ind = len(line) - len(s)
        if ind == 0 and s and not s.startswith("#"):
            in_server, in_clients = s.startswith("server:"), False
        elif in_server and ind == 2 and s and not s.startswith("#"):
            in_clients = s.startswith("clients:")
        elif in_clients and ind == 4:
            if s.startswith("teaspeak:"):
                line = "    teaspeak: 0"
                changes.append(f"server.clients.teaspeak: '{s}' -> 'teaspeak: 0'")
                done = True
            elif s.startswith("teamspeak_message_type:") or (anchor is None and s.startswith("teamspeak:")):
                anchor = len(out)
        out.append(line)
    if not done:
        if anchor is None:
            raise ValueError("no encuentro server.clients (teamspeak / teamspeak_message_type) en config.yml")
        block = ["    #Description:",
                 "    #  Allow/disallow the TeaSpeak - Client to join the server.",
                 "    #Notes:",
                 "    #  This option could be reloaded while the instance is running.",
                 "    #The value must be a positive numeric value between 0 and 1",
                 "    teaspeak: 0"]
        out[anchor + 1:anchor + 1] = block
        changes.append("server.clients.teaspeak: 0 insertada (no existia)")
    return "\n".join(out), changes


def _strip_known(tree: dict):
    """Copia del arbol sin las claves que cambiamos (para comprobar que NADA mas cambia)."""
    import copy
    t = copy.deepcopy(tree)
    t.get("general", {}).get("database", {}).pop("url", None)
    t.get("log", {}).pop("instance_logs_url", None)
    t.get("server", {}).get("clients", {}).pop("teaspeak", None)
    return t


def validate_config(old_src: str, new_src: str, new_pw: str):
    """Parseo YAML en seco del config editado. Devuelve lista de problemas (vacia = OK). Sin secretos."""
    import yaml
    probs = []
    try:
        old, new = yaml.safe_load(old_src), yaml.safe_load(new_src)
    except yaml.YAMLError as e:
        return [f"YAML invalido: {str(e).splitlines()[0][:120]}"]
    try:
        if new["server"]["clients"]["teaspeak"] not in (0, False):
            probs.append("server.clients.teaspeak no es 0 tras la edicion")
        for path, val in (("general.database.url", new["general"]["database"]["url"]),
                          ("log.instance_logs_url", new["log"].get("instance_logs_url", ""))):
            if val and f"://{PG_ROLE}:" in val and f"://{PG_ROLE}:{new_pw}@" not in val:
                probs.append(f"{path} no lleva la contrasena nueva")
        if f"://{PG_ROLE}:{new_pw}@" not in new["general"]["database"]["url"]:
            probs.append("general.database.url no lleva la contrasena nueva")
    except (KeyError, TypeError) as e:
        probs.append(f"falta la clave {e}")
    if _strip_known(old) != _strip_known(new):
        probs.append("la edicion cambia algo mas que las 3 claves previstas")
    return probs


def current_password(src: str) -> str | None:
    m = PW_RE.search(src)
    return m.group(2) if m else None


def write_config(text: str, ref: str):
    """Escritura atomica conservando dueno y modo del fichero original."""
    st = os.stat(ref)
    tmp = CONFIG + ".tmp_build1"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, CONFIG)


def config_teaspeak_value():
    """Valor de server.clients.teaspeak en el config.yml en disco (por si TeaSpeak reescribe el fichero)."""
    import yaml
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return yaml.safe_load(f)["server"]["clients"].get("teaspeak", "AUSENTE")
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}"


# ───────────────────────── rol de PostgreSQL ─────────────────────────
def role_verifier() -> str | None:
    v = base.psql(f"SELECT rolpassword FROM pg_authid WHERE rolname='{PG_ROLE}'", db="postgres")
    return v or None


def md5_verifier(pw: str) -> str:
    return "md5" + hashlib.md5((pw + PG_ROLE).encode()).hexdigest()


def set_role_verifier(verifier: str) -> bool:
    """ALTER ROLE con un verificador (md5... o SCRAM-SHA-256$...), nunca con la contrasena en claro.
    El SQL va por stdin (no por argv) y la sesion desactiva el registro de sentencias."""
    if not re.fullmatch(r"md5[0-9a-f]{32}|SCRAM-SHA-256\$[A-Za-z0-9+/=:$]+", verifier or ""):
        log("[pg] verificador con formato inesperado: NO se ejecuta el ALTER")
        return False
    sql = ("SET log_statement='none';\nSET log_min_error_statement='panic';\nSET log_min_duration_statement=-1;\n"
           f"ALTER ROLE {PG_ROLE} PASSWORD '{verifier}';\n")
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", "postgres"],
                       input=sql, capture_output=True, text=True, cwd="/tmp")
    ok = r.returncode == 0 and role_verifier() == verifier
    log(f"[pg] ALTER ROLE {PG_ROLE}: ok={ok}" + ("" if ok else f" rc={r.returncode}"))
    return ok


def pg_login_ok(pw: str, db: str) -> bool:
    """Login real por TCP 127.0.0.1 (el mismo camino que TeaSpeak: pg_hba md5). Contrasena por entorno, no argv."""
    env = dict(os.environ, PGPASSWORD=pw, PGCONNECT_TIMEOUT="10")
    r = subprocess.run(["psql", "-X", "-tA", "-h", "127.0.0.1", "-U", PG_ROLE, "-d", db, "-c", "SELECT 1"],
                       capture_output=True, text=True, env=env, cwd="/tmp")
    return r.returncode == 0 and r.stdout.strip() == "1"


def other_role_sessions():
    """Sesiones del rol teaspeak que NO son de TeaSpeak (127.0.0.1): p. ej. pgAdmin del admin."""
    return base.psql("SELECT coalesce(string_agg(DISTINCT coalesce(host(client_addr),'local')||' '||"
                     "coalesce(nullif(application_name,''),'-'), '; '),'') FROM pg_stat_activity "
                     f"WHERE usename='{PG_ROLE}' AND coalesce(host(client_addr),'') <> '127.0.0.1'", db="postgres")


# ───────────────────────── flujo ─────────────────────────
async def main():
    log(f"=== ts_maint_build1 {'(CHECK)' if CHECK else '(REAL)'} ===")
    problems, pending_b1 = [], []

    # 1) binario nuevo
    if not os.path.exists(NEW) or md5(NEW) != NEW_MD5:
        problems.append("binario Build 1 ausente o con hash inesperado")
    else:
        r = bash(f"LD_LIBRARY_PATH=/opt/teaspeak/libs ldd {NEW} | grep -c 'not found' || true")
        if r.stdout.strip() not in ("0", ""):
            problems.append(f"al binario Build 1 le faltan librerias ({r.stdout.strip()})")

    # 2) base B1 en vivo (Build 1 se construyo sobre e02aefb)
    live = md5(LIVE)
    ident = letterid_identity()
    if live != EXPECTED_LIVE:
        pending_b1.append(f"el binario vivo es {live[:8]}, se espera el de B1 {EXPECTED_LIVE[:8]}")
    if ident != "YES":
        pending_b1.append(f"letters.letterid identity={ident} (B1 la deja en YES)")

    # 3) config: edicion + parseo en seco (con una contrasena candidata que se descarta en --check)
    with open(CONFIG, encoding="utf-8") as f:
        old_src = f.read()
    new_pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
    try:
        new_src, changes = edit_config(old_src, new_pw)
        vprobs = validate_config(old_src, new_src, new_pw)
    except ValueError as e:
        changes, vprobs = [], [str(e)]
    problems += [f"config: {p}" for p in vprobs]
    log(f"config: cambios previstos: {changes}")

    # 4) PG: la contrasena actual del config funciona, y el verificador actual se puede guardar para rollback
    old_pw = current_password(old_src)
    if not old_pw or not pg_login_ok(old_pw, "teaspeak"):
        problems.append("la contrasena ACTUAL del config.yml no permite entrar a PG como teaspeak (no se rota a ciegas)")
    old_ver = role_verifier()
    if not old_ver or not re.fullmatch(r"md5[0-9a-f]{32}|SCRAM-SHA-256\$.+", old_ver):
        problems.append("no se puede leer el verificador actual del rol (rollback de la contrasena imposible)")
    others = other_role_sessions()
    if others:
        log(f"AVISO: otras sesiones con el rol {PG_ROLE} (necesitaran la contrasena nueva): {others}")

    expected, clients = await base.count_clients_expected()
    log(f"vservers={expected} (esperados {EXPECTED_VS}) clientes={clients} live_md5={live[:8]} "
        f"letterid_identity={ident} groups_seq_ok={groups_seq_ok()} lock_test_rows={lock_test_rows()}")
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
        tmp = f"/tmp/ts_maint_build1_check_{os.getpid()}.sql.gz"
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
        log(f"[check] config.yml actual: server.clients.teaspeak={config_teaspeak_value()}")
        log(f"[check] rol {PG_ROLE}: verificador actual tipo={(old_ver or '')[:3] or '?'}; el nuevo sera md5 "
            f"(password_encryption={base.psql('SHOW password_encryption', db='postgres')})")
        log("[check] SIN ALTER, SIN reinicio, SIN WhatsApp. La contrasena candidata se descarta.")
        for p in pending_b1:
            log(f"[check] PENDIENTE B1: {p} -> el modo real ABORTARA hasta que B1 este desplegado y verificado")
        if problems:
            for p in problems:
                log(f"[check] FALLO: {p}")
            log("[check] NO ejecutar.")
            return 2
        log("[check] OK" + (" (salvo PENDIENTE B1)" if pending_b1 else "") + ". No se toco produccion.")
        return 0

    # ── REAL ──
    if problems or pending_b1:
        msg = "; ".join(problems + pending_b1)[:300]
        log(f"ABORTADO sin tocar nada: {msg}")
        wa_send(f"🚨 [Mantenimiento Build 1] Abortado ANTES de empezar (no se toco nada): {msg}")
        return 2

    wa_send("🛠️ [Aviso] Mantenimiento de TeaSpeak en ~5 min. Aviso por poke a los conectados. "
            "Build 1: correcciones de integridad de datos y seguridad + rotacion de la contrasena de la BD "
            "+ cliente TeaSpeak desactivado. Corte de voz ~1-2 min; todos reconectan solos.")
    poked = await base.warn_poke("🛠️ Maintenance in ~5 min / Mantenimiento en ~5 min / Manutenção em ~5 min: "
                                 "voice drop ~1-2 min, you will reconnect automatically.")
    log(f"pokeados: {poked}")
    await asyncio.sleep(WARN_SECONDS)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_bin = f"{LIVE}.bak_{stamp}"
    bak_cfg = f"{CONFIG}.bak_{stamp}"
    touched = role_altered = False
    since, since_epoch, start_epoch = None, time.time(), None
    try:
        wa_send(f"🔧 [1/5] Iniciando. Backup del binario/config/rol + dump de la BD teaspeak (poked={poked})...")
        shutil.copy2(LIVE, bak_bin)
        shutil.copy2(CONFIG, bak_cfg)
        os.chmod(bak_cfg, 0o600)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ver_file = f"{BACKUP_DIR}/teaspeak_role_verifier_{stamp}.txt"
        fd = os.open(ver_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(old_ver + "\n")
        dump = f"{BACKUP_DIR}/teaspeak_premaint_{stamp}.sql.gz"
        ok_dump, err = pg_dump_to(dump)
        if not ok_dump:
            raise RuntimeError(f"pg_dump fallo antes de tocar nada: {err}")
        # el config de hoy puede haber cambiado desde el check inicial: se re-edita sobre el fichero actual
        with open(CONFIG, encoding="utf-8") as f:
            old_src = f.read()
        new_src, changes = edit_config(old_src, new_pw)
        vprobs = validate_config(old_src, new_src, new_pw)
        if vprobs:
            raise RuntimeError(f"edicion de config invalida: {vprobs}")
        log(f"backup bin -> {bak_bin} ; config -> {bak_cfg} ; rol -> {ver_file} ; dump -> {dump}")

        wa_send("🔄 [2/5] Parando TeaSpeak, cambiando binario + config y rotando la contrasena de la BD...")
        since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        since_epoch = time.time()
        touched = True
        run(f"systemctl stop {TS_SERVICE}")
        shutil.copy2(NEW, LIVE + ".tmp")
        run(f"chown teaspeak:teaspeak {LIVE}.tmp && chmod 755 {LIVE}.tmp && mv -f {LIVE}.tmp {LIVE}")
        if md5(LIVE) != NEW_MD5:
            raise RuntimeError("el binario copiado no tiene el md5 esperado")
        write_config(new_src, CONFIG)
        # B15: ALTER justo antes del arranque (TeaSpeak parado)
        role_altered = True
        if not set_role_verifier(md5_verifier(new_pw)):
            raise RuntimeError("ALTER ROLE fallo")
        start_epoch = time.time()
        run(f"systemctl start {TS_SERVICE}")
        log("teaspeak arrancado con Build 1 + config nuevo; verificando salud...")

        ok, info = await base.ts_healthy(expected)
        if not ok:
            raise RuntimeError(f"health-check fallo tras el cambio: {info}")
        await asyncio.sleep(10)
        if not service_active():
            raise RuntimeError("TeaSpeak se cayo a los pocos segundos de arrancar")
        if not (pg_login_ok(new_pw, "teaspeak") and pg_login_ok(new_pw, "teaspeak_logs")):
            raise RuntimeError("el login a PG con la contrasena nueva no funciona")

        wa_send(f"🟢 [3/5] TeaSpeak ARRIBA y estable ({info}). Reinicio el bot...")
        run(f"systemctl restart {BOT_SERVICE}")
        await asyncio.sleep(8)
        wa_send("✅ [4/5] Bot reiniciado, instancias reconectando...")
        await asyncio.sleep(40)

        clients, vs = await base.count_clients()
        new_log, old_log = ts_run_logs()
        res = {
            "vservers": vs,
            "groups_seq_ok": groups_seq_ok(),
            "lock_test_rows": lock_test_rows(),
            "letterid_identity": letterid_identity(),
            "pg_log_errors": pg_log_errors(since),
            "ts_log_crashes_new": ts_log_crashes(new_log),
            "crash_dumps_new": crash_dumps_since(start_epoch),
            "pg_login_new_pw": pg_login_ok(new_pw, "teaspeak"),
            "config_teaspeak": config_teaspeak_value(),
        }
        want = {"vservers": expected, "groups_seq_ok": "t", "lock_test_rows": "1", "letterid_identity": "YES",
                "pg_log_errors": 0, "ts_log_crashes_new": 0, "crash_dumps_new": 0, "pg_login_new_pw": True,
                "config_teaspeak": 0}
        bad = [k for k in want if res[k] != want[k]]
        info_stop = (f"parada del binario viejo: crash_dumps={crash_dumps_since(since_epoch) - res['crash_dumps_new']}, "
                     f"crash en su log={ts_log_crashes(old_log)} (esperable: el fix T01 solo aplica a la PROXIMA parada)")
        auth_fail = pg_auth_failures(since)
        log(f"post-checks: {res} ; malos={bad} ; {info_stop} ; auth_fail_teaspeak={auth_fail} ; "
            f"logs: nuevo={new_log} anterior={old_log}")
        flag = "⚠️ REVISAR: " + ", ".join(bad) + ". " if bad else ""
        wa_send(f"🎉 [5/5] Mantenimiento Build 1 COMPLETADO. {flag}{clients} clientes online ({vs}/{expected} vservers). "
                f"groups_seq_ok={res['groups_seq_ok']} lock_test={res['lock_test_rows']} "
                f"letterid={res['letterid_identity']} pg_err={res['pg_log_errors']} "
                f"crash_nuevos={res['ts_log_crashes_new']}/{res['crash_dumps_new']} pg_login_nueva={res['pg_login_new_pw']} "
                f"clients.teaspeak={res['config_teaspeak']} auth_fail_teaspeak={auth_fail}. "
                "Contrasena de la BD rotada (solo en config.yml); pgAdmin necesita la nueva.")
        log(f"=== COMPLETADO {'CON AVISOS' if bad else 'OK'} ===")
        return 0 if not bad else 4

    except Exception as ex:
        reason = str(ex)[:200]
        log(f"!!! FALLO: {reason} -> ROLLBACK")
        try:
            if not touched:
                wa_send(f"❌ [Mantenimiento Build 1] Abortado ANTES de tocar TeaSpeak: {reason}. "
                        "No se reinicio nada; TeaSpeak y bot siguen igual.")
                log("=== ABORTADO SIN TOCAR NADA ===")
                return 1
            run(f"systemctl stop {TS_SERVICE}")
            shutil.copy2(bak_bin, LIVE)
            run(f"chown teaspeak:teaspeak {LIVE} && chmod 755 {LIVE}")
            shutil.copy2(bak_cfg, CONFIG)
            run(f"chown teaspeak:teaspeak {CONFIG} && chmod 640 {CONFIG}")
            role_ok = True
            if role_altered:
                role_ok = set_role_verifier(old_ver)
            run(f"systemctl start {TS_SERVICE}")
            ok, info = await base.ts_healthy(expected)
            if ok and role_ok:
                run(f"systemctl restart {BOT_SERVICE}")
                wa_send(f"❌ [Mantenimiento Build 1] Fallo: {reason}. REVERTI binario, config y contrasena de la BD. "
                        f"TeaSpeak ARRIBA y estable ({info}). Bot reiniciado.")
                log("=== ROLLBACK OK ===")
                return 1
            wa_send(f"🚨 [Mantenimiento Build 1] CRITICO: fallo el cambio Y el rollback ({info}; rol_restaurado={role_ok}). "
                    "INTERVENCION MANUAL YA (RUNBOOK paso 1, rollback manual).")
            log(f"=== ROLLBACK FALLIDO (rol_restaurado={role_ok}) ===")
            return 3
        except Exception as ex2:
            wa_send(f"🚨 [Mantenimiento Build 1] CRITICO: excepcion en el rollback ({str(ex2)[:150]}). INTERVENCION MANUAL YA.")
            log(f"=== ROLLBACK EXCEPCION: {ex2} ===")
            return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
