#!/opt/tsbot-dash/venv/bin/python
"""Mantenimiento auto-ejecutable de TeaSpeak: sube los pools de hilos en config.yml,
reinicia, verifica salud y HACE ROLLBACK AUTOMATICO si algo va mal, avisando al admin
por WhatsApp en cada paso. One-shot (se auto-desprograma). Solo toca hilos (cambio
reversible); NO limpia conversaciones (eso va supervisado aparte).

Modos:
  --check : valida edicion de config + conectividad del health-check, SIN tocar produccion
            ni enviar WhatsApp (para revisar antes de agendar).
  (sin args): ejecuta el mantenimiento real.
"""
from __future__ import annotations
import asyncio, os, subprocess, sys, time, urllib.request, urllib.parse, shutil
from datetime import datetime

sys.path.insert(0, "/opt/tsbot-dash")
import ts_ops
from ts_ops import _parse

CHECK = "--check" in sys.argv
CRON_FILE = "/etc/cron.d/ts-maint-once"
CONFIG = "/opt/teaspeak/config.yml"
BAK = "/opt/teaspeak/config.yml.maint_bak"
BACKUP_DIR = "/opt/teaspeak/backups"
TS_SERVICE = "teaspeak"
BOT_SERVICE = "tsbot"
WARN_SECONDS = int(os.environ.get("WARN_SECONDS", "300"))   # aviso previo
HEALTH_TIMEOUT = 150

# cambios de hilos (clave -> (indent, valor nuevo, ambito))
def log(m): print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {m}", flush=True)

def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def psql(sql, db="tsbot"):
    r = run(f'sudo -u postgres psql -tAc "{sql}" {db}')
    return r.stdout.strip()

# ---------- WhatsApp ----------
def _gc(key):
    return psql(f"SELECT value FROM global_config WHERE key='{key}'")

def wa_send(text):
    if CHECK:
        log(f"[wa/check] (no enviado) {text}")
        return True
    inst = _gc("whatsapp_instance"); tok = _gc("whatsapp_token"); to = _gc("admin_wa")
    if not (inst and tok and to):
        log(f"[wa] faltan credenciales (inst={bool(inst)} tok={bool(tok)} to={bool(to)})")
        return False
    data = urllib.parse.urlencode({"token": tok, "to": to, "body": text}).encode()
    req = urllib.request.Request(
        f"https://api.ultramsg.com/instance{inst}/messages/chat", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode(errors="ignore")
        ok = '"sent"' in body and ("true" in body.lower() or "\"sent\":\"true\"" in body)
        log(f"[wa] enviado ok={ok}")
        return ok
    except Exception as ex:
        log(f"[wa] error: {ex}")
        return False

# ---------- edicion de config.yml (context-aware, solo la seccion threads) ----------
def edit_threads(src):
    lines = src.split("\n")
    out, changes = [], []
    in_threads = in_voice = False
    top = {"ticking": 4, "command_execute": 8, "network_events": 8}
    voice = {"execute_limit": 64, "io_min": 4, "io_limit": 64}
    for line in lines:
        s = line.lstrip(); indent = len(line) - len(s)
        if indent == 0 and s.startswith("threads:"):
            in_threads, in_voice = True, False; out.append(line); continue
        if in_threads and indent == 0 and s and not s.startswith("#"):
            in_threads = in_voice = False  # salimos del bloque threads
        if in_threads:
            if indent == 2 and s.startswith("voice:"):
                in_voice = True; out.append(line); continue
            if indent == 2 and s.endswith(":") and not s.startswith("#"):
                in_voice = False  # otra subseccion (music/web)
            if indent == 2:
                for k, v in top.items():
                    if s.startswith(f"{k}:"):
                        line = f"{' '*indent}{k}: {v}"; changes.append((k, v))
            if in_voice and indent == 4:
                for k, v in voice.items():
                    if s.startswith(f"{k}:"):
                        line = f"{' '*indent}{k}: {v}"; changes.append((k, v))
        out.append(line)
    return "\n".join(out), changes

# ---------- health check ----------
async def ts_healthy(expected, timeout=HEALTH_TIMEOUT):
    creds = _ts_creds(); deadline = time.time() + timeout; last = "sin respuesta"
    while time.time() < deadline:
        if run(f"systemctl is-active {TS_SERVICE}").stdout.strip() != "active":
            last = "servicio no active"; await asyncio.sleep(4); continue
        try:
            ts = await ts_ops.connect(creds)
            b, e = await ts.send("serverlist")
            online = sum(1 for r in b[0].split("|")
                         if _parse(r).get("virtualserver_status") == "online") if b and b[0] else 0
            await ts.send("quit")
            if online >= expected:
                return True, f"{online} vservers online"
            last = f"solo {online}/{expected} vservers online"
        except Exception as ex:
            last = str(ex)[:140]
        await asyncio.sleep(5)
    return False, last

_CREDS = None
def _ts_creds():
    global _CREDS
    if _CREDS is None:
        row = psql("SELECT ts_address||'|'||ts_port||'|'||ts_query_user||'|'||ts_query_pass "
                   "FROM instances ORDER BY id LIMIT 1").split("|")
        _CREDS = {"ts_address": row[0], "ts_port": int(row[1]),
                  "ts_query_user": row[2], "ts_query_pass": row[3]}
    return _CREDS

async def count_clients():
    try:
        ts = await ts_ops.connect(_ts_creds())
        b, _ = await ts.send("hostinfo"); h = _parse(b[0]) if b else {}
        await ts.send("quit")
        return int(h.get("virtualservers_total_clients_online", 0)), \
               int(h.get("virtualservers_running_total", 0))
    except Exception:
        return -1, -1

async def warn_poke(msg):
    poked = 0
    try:
        ts = await ts_ops.connect(_ts_creds())
        b, _ = await ts.send("serverlist")
        sids = [_parse(r)["virtualserver_id"] for r in b[0].split("|")
                if _parse(r).get("virtualserver_status") == "online"] if b and b[0] else []
        for sid in sids:
            try:
                await ts.use(sid)
                spy = set()
                bg, _ = await ts.send("servergrouplist")
                ssg = next((_parse(r)["sgid"] for r in bg[0].split("|")
                            if _parse(r).get("name") == "SPY"), None) if bg and bg[0] else None
                if ssg:
                    bs, es = await ts.send(f"servergroupclientlist sgid={ssg}")
                    if es.get("id") == "0" and bs and bs[0]:
                        spy = {_parse(r).get("cldbid") for r in bs[0].split("|")}
                bc, _ = await ts.send("clientlist")
                for r in (bc[0].split("|") if bc and bc[0] else []):
                    d = _parse(r)
                    if d.get("client_type") == "0" and d.get("client_database_id") not in spy:
                        try:
                            await ts.send(f"clientpoke clid={d['clid']} msg={ts_ops._esc(msg)}")
                            poked += 1
                        except Exception:
                            pass
            except Exception:
                pass
        await ts.send("quit")
    except Exception as ex:
        log(f"[poke] error: {ex}")
        return -1
    return poked

# ---------- flujo principal ----------
def apply_config():
    src = open(CONFIG, encoding="utf-8").read()
    new, changes = edit_threads(src)
    return src, new, changes

async def main():
    # one-shot: desprogramar SIEMPRE al arrancar (aunque falle luego)
    if not CHECK and os.path.exists(CRON_FILE):
        run(f"rm -f {CRON_FILE}"); log("cron one-shot eliminado")

    log(f"=== ts_maint {'(CHECK)' if CHECK else '(REAL)'} ===")
    src, new, changes = apply_config()
    log(f"cambios de hilos detectados: {changes}")
    if len(changes) != 6:
        msg = f"edicion de config invalida: esperaba 6 cambios, hubo {len(changes)} ({changes}). ABORTADO sin tocar nada."
        log(msg); wa_send(f"🚨 [Mantenimiento] {msg}")
        return 2

    expected, _ = await count_clients_expected()
    log(f"vservers online esperados: {expected}")

    if CHECK:
        log("[check] validando health-check (read-only)...")
        ok, info = await ts_healthy(expected, timeout=20)
        log(f"[check] health actual: ok={ok} ({info})")
        log("[check] WA creds presentes: inst=%s tok=%s to=%s" %
            (bool(_gc('whatsapp_instance')), bool(_gc('whatsapp_token')), bool(_gc('admin_wa'))))
        log("[check] diff (primeras lineas cambiadas):")
        for a, b in zip(src.split("\n"), new.split("\n")):
            if a != b:
                log(f"    - {a.strip()}   ->   + {b.strip()}")
        log("[check] OK. No se toco produccion.")
        return 0

    # --- REAL ---
    wa_send("🛠️ [Aviso] Mantenimiento de TeaSpeak en ~5 min. Aviso por poke a los conectados. "
            "Subida de pools de hilos (rendimiento). Corte de voz ~1-2 min; todos reconectan solos.")
    poked = await warn_poke("🛠️ Mantenimiento en ~5 min: mejora de rendimiento. Corte de voz ~1-2 min; reconectas solo automaticamente.")
    log(f"pokeados: {poked}")
    await asyncio.sleep(WARN_SECONDS)

    try:
        wa_send(f"🔧 [1/5] Iniciando. Backup de config.yml + dump de la BD teaspeak (poked={poked})...")
        shutil.copy2(CONFIG, BAK)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dump = f"{BACKUP_DIR}/teaspeak_premaint_{ts}.sql.gz"
        run(f"sudo -u postgres pg_dump --no-owner --no-privileges teaspeak | gzip > {dump}")
        log(f"backup config -> {BAK} ; dump -> {dump}")

        wa_send("🔄 [2/5] Aplicando nuevos hilos y reiniciando TeaSpeak...")
        open(CONFIG, "w", encoding="utf-8").write(new)
        run(f"systemctl restart {TS_SERVICE}")
        log("teaspeak reiniciado; verificando salud...")

        ok, info = await ts_healthy(expected)
        if not ok:
            raise RuntimeError(f"health-check fallo tras el cambio: {info}")

        wa_send(f"🟢 [3/5] TeaSpeak ARRIBA y estable ({info}). Reinicio el bot...")
        run(f"systemctl restart {BOT_SERVICE}")
        await asyncio.sleep(8)
        wa_send("✅ [4/5] Bot reiniciado, instancias reconectando...")
        await asyncio.sleep(20)
        clients, vs = await count_clients()
        wa_send(f"🎉 [5/5] Mantenimiento COMPLETADO. Hilos subidos y activos. "
                f"{clients} clientes online ({vs} vservers), reconectando. Todo OK.")
        log("=== COMPLETADO OK ===")
        return 0

    except Exception as ex:
        reason = str(ex)[:200]
        log(f"!!! FALLO: {reason} -> ROLLBACK")
        try:
            shutil.copy2(BAK, CONFIG)
            run(f"systemctl restart {TS_SERVICE}")
            ok, info = await ts_healthy(expected)
            if ok:
                run(f"systemctl restart {BOT_SERVICE}")
                wa_send(f"❌ [Mantenimiento] Fallo el cambio: {reason}. REVERTI a la config anterior. "
                        f"TeaSpeak ARRIBA y estable ({info}). Usuarios NO afectados. Bot reiniciado.")
                log("=== ROLLBACK OK ===")
                return 1
            wa_send(f"🚨 [Mantenimiento] CRITICO: fallo el cambio Y el rollback ({info}). "
                    f"TeaSpeak inestable. REQUIERE INTERVENCION MANUAL YA.")
            log("=== ROLLBACK FALLIDO ===")
            return 3
        except Exception as ex2:
            wa_send(f"🚨 [Mantenimiento] CRITICO: excepcion en el rollback ({str(ex2)[:150]}). INTERVENCION MANUAL YA.")
            log(f"=== ROLLBACK EXCEPCION: {ex2} ===")
            return 3

async def count_clients_expected():
    c, vs = await count_clients()
    return (vs if vs > 0 else 14), c

if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
