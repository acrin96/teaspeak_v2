#!/opt/tsbot-dash/venv/bin/python
"""Paso posterior de la ventana Build 2 (29-sep-2026). Cron de SISTEMA one-shot a las 10:20 CEST
(/etc/cron.d/ts-maint-build2-post-once), despues de ts_maint_build2.py (09:57).

1. Espera (max 30 min) a que ts_maint_build2.py haya terminado.
2. Lee /var/log/ts_maint_build2.log (ultima ejecucion REAL) y /root/window-build2/state_build2.json.
3. SOLO si el log tiene "=== COMPLETADO OK ===":
     - fusiona build2-rendimiento -> main en acrin96/teaspeak_v2-src y acrin96/teaspeak_v2, y
       ventana-20260929 -> main en acrin96/TsBot-Deploy (fast-forward; si no se puede, merge --no-ff en un
       worktree temporal; si hay conflicto se aborta sin tocar main). Nunca --force.
     - publica la release v1.4.21-beta-3-build2 (latest) con los MISMOS nombres de asset que build1
       (preparados por stage_release_build2.sh en /root/window-build2/release): borrador -> assets -> publicar.
     - verifica una descarga de releases/latest: el TeaSpeakServer del bundle debe tener el md5 de Build 2.
   Si algo de GitHub falla no se reintenta de forma destructiva: se informa en el resumen.
   Con "COMPLETADO CON AVISOS" NO se publica (hay que revisar antes): luego, a mano, --publish-only.
4. Manda SIEMPRE (tambien si hubo rollback, aborto o no se ejecuto) un WhatsApp final SOLO al admin
   (admin_wa) con todo lo aplicado, punto por punto y con su resultado.

Modos:
  --dry           : no manda WhatsApp ni escribe en GitHub (solo lecturas: git fetch, GETs de la API);
                    imprime el resumen y lo que haria.
  --publish-only  : solo la parte de GitHub (tras revisar unos avisos) + WhatsApp corto con su resultado.
  --log/--state   : otros ficheros (pruebas). --no-wait: no esperar al proceso de mantenimiento.
  --date AAAA-MM-DD: fecha esperada de la ejecucion (por defecto hoy).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, "/opt/teaspeak/scripts")
import ts_maint as base  # log, wa_send (solo al admin_wa)

ap = argparse.ArgumentParser()
ap.add_argument("--dry", action="store_true")
ap.add_argument("--publish-only", action="store_true")
ap.add_argument("--log", default="/var/log/ts_maint_build2.log")
ap.add_argument("--state", default="/root/window-build2/state_build2.json")
ap.add_argument("--no-wait", action="store_true")
ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
args = ap.parse_args()
DRY = args.dry
base.CHECK = DRY          # wa_send no envia en --dry
log = base.log

B1_MD5 = "ef2c609533996e81044b33c7e3c09d71"
B2_MD5 = "ae2a571c74fa997f84d215104af9f169"
B2_SHA256 = "b40d9872e8175190bb32b69612eafb18a355ad6122b35f5fe2b6a597ad923916"
TAG = "v1.4.21-beta-3-build2"
GH_REPO = "acrin96/teaspeak_v2"
REL_DIR = "/root/window-build2/release"
BUNDLE = "teaspeak_v2_1.4.21-beta-3_linux_amd64.tar.gz"
GEO = "teaspeak_v2_geoloc.tar.gz"
ASSETS = [(BUNDLE, "application/gzip"), (GEO, "application/gzip"), ("SHA256SUMS", "text/plain")]
REPOS = [("teaspeak_v2-src", "/root/work/teaspeak_v2-src", "build2-rendimiento"),
         ("teaspeak_v2", "/root/work/teaspeak_v2", "build2-rendimiento"),
         ("TsBot-Deploy", "/root/work/TsBot-Deploy", "ventana-20260929")]
GIT_ID = ["-c", "user.name=acrin96", "-c", "user.email=acrin96@users.noreply.github.com"]
COAUTHOR = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")


def clean(s, n=140):
    """Texto corto y sin credenciales para el resumen."""
    s = re.sub(r"https://[^\s@/]+@", "https://***@", str(s or ""))
    s = re.sub(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+", "***", s)
    return " ".join(s.split())[:n]


# ───────────────────────── lectura del resultado ─────────────────────────
def maint_running():
    return subprocess.run(["pgrep", "-f", "window-build2/ts_maint_build2.py"], capture_output=True).returncode == 0


def read_run():
    """Texto de la ultima ejecucion REAL del dia pedido ('' si no hay)."""
    try:
        txt = open(args.log, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    idx = txt.rfind("=== ts_maint_build2 (REAL) ===")
    if idx < 0:
        return ""
    line_start = txt.rfind("\n", 0, idx) + 1
    part = txt[line_start:]
    return part if part.startswith(f"[{args.date}") else ""


def read_state():
    try:
        st = json.load(open(args.state, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return st if str(st.get("started", "")).startswith(args.date) else {}


MARKERS = [("=== COMPLETADO OK ===", "ok"), ("=== COMPLETADO CON AVISOS ===", "avisos"),
           ("=== ROLLBACK OK ===", "rollback_ok"), ("=== ROLLBACK FALLIDO", "rollback_fallido"),
           ("=== ROLLBACK EXCEPCION", "rollback_fallido"), ("=== ABORTADO SIN TOCAR NADA", "abortado")]


def detect_outcome(run_txt):
    if not run_txt:
        return "no_ejecutado"
    for m, o in MARKERS:
        if m in run_txt:
            return o
    return "incompleto"


def fail_reason(run_txt, state):
    if state.get("reason"):
        return clean(state["reason"], 200)
    m = re.findall(r"(?:!!! FALLO: |ABORTADO sin tocar nada: )(.*?)(?: -> ROLLBACK)?$", run_txt, re.M)
    return clean(m[-1], 200) if m else "sin detalle en el log"


# ───────────────────────── git / GitHub ─────────────────────────
def git(d, *a, timeout=180):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, timeout=timeout, env=env)


def merge_to_main(name, d, br):
    if git(d, "fetch", "-q", "origin").returncode != 0:
        return "FALLO (git fetch)"
    if git(d, "rev-parse", "-q", "--verify", f"origin/{br}").returncode != 0:
        return f"FALLO (no existe origin/{br})"
    head = git(d, "rev-parse", "--short", f"origin/{br}").stdout.strip()
    if git(d, "merge-base", "--is-ancestor", f"origin/{br}", "origin/main").returncode == 0:
        return f"OK (ya estaba en main, {head})"
    if git(d, "merge-base", "--is-ancestor", "origin/main", f"origin/{br}").returncode == 0:
        if DRY:
            return f"(dry) haria fast-forward de main a {head}"
        r = git(d, "push", "origin", f"refs/remotes/origin/{br}:refs/heads/main")
        return f"OK (fast-forward a {head})" if r.returncode == 0 else f"FALLO (push: {clean(r.stderr, 90)})"
    if DRY:
        return f"(dry) main avanzo por su lado: haria merge --no-ff de {br} ({head})"
    wt = f"/root/work/merge-{name}-{STAMP}"
    r = git(d, "worktree", "add", "--detach", wt, "origin/main")
    if r.returncode != 0:
        return f"FALLO (worktree: {clean(r.stderr, 80)})"
    try:
        msg = f"Merge {br} en main (ventana 29-sep-2026, Build 2 en produccion)\n\n{COAUTHOR}"
        r = git(wt, *GIT_ID, "merge", "--no-ff", "--no-edit", "-m", msg, f"origin/{br}")
        if r.returncode != 0:
            git(wt, "merge", "--abort")
            return "FALLO (conflicto de merge; main sin tocar, fusionar a mano)"
        r = git(wt, "push", "origin", "HEAD:refs/heads/main")
        sha = git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
        return f"OK (merge {sha})" if r.returncode == 0 else f"FALLO (push del merge: {clean(r.stderr, 80)})"
    finally:
        git(d, "worktree", "remove", "--force", wt)


def gh_token():
    try:
        for line in open("/root/.git-credentials", encoding="utf-8"):
            m = re.match(r"https://[^:]+:([^@]+)@github\.com/?", line.strip())
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def api(method, url, data=None, raw=None, ctype="application/json", timeout=60):
    tok = gh_token()
    if not tok:
        return -1, {"error": "sin credencial de GitHub"}
    headers = {"Authorization": f"token {tok}", "Accept": "application/vnd.github+json",
               "User-Agent": "tsbot-post-build2"}
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    if body is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read()
            return r.status, (json.loads(content) if content else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": clean(e.read().decode(errors="ignore"), 160)}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": clean(e, 160)}


def bundle_bin_md5(path):
    with tarfile.open(path, "r:gz") as t:
        f = t.extractfile("teaspeak_v2_bundle/TeaSpeakServer")
        h = hashlib.md5()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest()


def staged_ok():
    for name, _ in ASSETS:
        if not os.path.exists(f"{REL_DIR}/{name}"):
            return False, f"falta {REL_DIR}/{name}"
    got = bundle_bin_md5(f"{REL_DIR}/{BUNDLE}")
    if got != B2_MD5:
        return False, f"el bundle preparado trae {got[:8]}, no Build 2"
    sums = {}
    for line in open(f"{REL_DIR}/SHA256SUMS", encoding="utf-8"):
        p = line.split()
        if len(p) == 2:
            sums[p[1]] = p[0]
    for name in (BUNDLE, GEO):
        h = hashlib.sha256(open(f"{REL_DIR}/{name}", "rb").read()).hexdigest()
        if sums.get(name) != h:
            return False, f"SHA256SUMS no cuadra con {name}"
    return True, "assets preparados OK (binario Build 2)"


def release_body(src_sha):
    return (
        "Build 2 de TeaSpeak v2 (1.4.21-beta-3 + PostgreSQL): rendimiento y estabilidad. "
        "**En produccion desde el 29-sep-2026 10:02 CEST.**\n\n"
        f"Binario `TeaSpeakServer`: md5 `{B2_MD5}`, sha256 `{B2_SHA256}`.\n"
        f"Fuente: acrin96/teaspeak_v2-src `main` @ `{src_sha}` (rama build2-rendimiento; detalle en docs/BUILD2.md).\n"
        "Mismo bundle que v1.4.21-beta-3-build1 (libs, recursos, scripts) con el binario sustituido y la seccion "
        "`threads` de `config.template.yml` igual a la de produccion; mismos nombres de asset, asi que "
        "`install.sh` (releases/latest) instala Build 2.\n\n"
        "Cambios principales:\n"
        "- T13: `spin_mutex` gira y despues espera en el kernel (futex); un solo log \"Ticking of client\" por tick.\n"
        "- T14: `channeledit` difunde con el lock compartido; PacketStatistics cada 15 s (la perdida de paquetes "
        "que ve el cliente se promedia en ~15 s).\n"
        "- T15: `synchronous_commit=off` en las conexiones a PostgreSQL (se vuelve a `on` con "
        "`&synchronous_commit=on` en `database.url` e `instance_logs_url`).\n"
        "- T16: el limite global de conexiones se recarga a `connect_limit` por segundo.\n"
        "- T17: `restrict_nickname_charset` activado por defecto.\n"
        "- T11: conversaciones: se recarga la cabecera del bloque activo en vez de cerrarlo.\n"
        "- T21/T22: crashes en desconexiones y rechazos masivos (carrera en `clientdisconnect` y en el "
        "\"Flush thread\" de `close_connection`).\n"
        "- Hilos (config): `ticking 2`, `command_execute 4`, `network_events 2`, `voice.io_min 4`, "
        "`voice.io_limit 4` (85 -> ~59 hilos con 14 vservers).\n"
    )


def publish_release(target_sha, src_sha):
    """Devuelve (texto_resultado, ok)."""
    ok, info = staged_ok()
    if not ok:
        return f"FALLO ({info}); no se publico nada", False
    base_url = f"https://api.github.com/repos/{GH_REPO}"
    code, rel = api("GET", f"{base_url}/releases/tags/{TAG}")
    if code == 200 and not rel.get("draft"):
        return verify_download(f"{TAG} ya existia publicada")
    if DRY:
        code_l, latest = api("GET", f"{base_url}/releases/latest")
        return (f"(dry) {info}; crearia {TAG} (target {target_sha[:7]}) como latest, subiria {len(ASSETS)} assets "
                f"y verificaria la descarga; latest actual={latest.get('tag_name') if code_l == 200 else code_l}"), True
    if code == 200:
        rid = rel["id"]
    elif code == 404:
        code, rel = api("POST", f"{base_url}/releases", {
            "tag_name": TAG, "target_commitish": target_sha, "draft": True, "prerelease": False,
            "name": "TeaSpeak v2 1.4.21-beta-3 - Build 2 (rendimiento + crashes de desconexion masiva)",
            "body": release_body(src_sha)})
        if code != 201:
            return f"FALLO (crear borrador: HTTP {code} {rel.get('error', '')})", False
        rid = rel["id"]
    else:
        return f"FALLO (consultar el tag: HTTP {code} {rel.get('error', '')})", False
    have = {a["name"] for a in rel.get("assets", [])}
    for name, ctype in ASSETS:
        if name in have:
            continue
        with open(f"{REL_DIR}/{name}", "rb") as f:
            data = f.read()
        code, _ = api("POST", f"https://uploads.github.com/repos/{GH_REPO}/releases/{rid}/assets?name={name}",
                      raw=data, ctype=ctype, timeout=600)
        if code != 201:
            return f"FALLO (subir {name}: HTTP {code}); queda como BORRADOR, latest sigue siendo build1", False
    code, rel = api("PATCH", f"{base_url}/releases/{rid}", {"draft": False, "make_latest": "true"})
    if code != 200:
        return f"FALLO (publicar el borrador: HTTP {code}); latest sigue siendo build1", False
    return verify_download("publicada como latest")


def verify_download(prefix):
    code, latest = api("GET", f"https://api.github.com/repos/{GH_REPO}/releases/latest")
    latest_tag = latest.get("tag_name") if code == 200 else f"HTTP {code}"
    os.makedirs("/root/work", exist_ok=True)
    tmp = f"/root/work/post_build2_verify_{STAMP}.tar.gz"
    url = f"https://github.com/{GH_REPO}/releases/latest/download/{BUNDLE}"
    got = "?"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tsbot-post-build2"})
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        got = bundle_bin_md5(tmp)
    except Exception as e:  # noqa: BLE001
        got = f"error {clean(e, 60)}"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    ok = got == B2_MD5 and latest_tag == TAG
    if ok:
        return f"OK ({prefix}; descarga de releases/latest verificada: md5 Build 2)", True
    return f"FALLO ({prefix}; verificacion: latest={latest_tag}, md5 descargado={got[:8]})", False


def github_steps():
    """Devuelve lista de (punto, resultado)."""
    items = []
    for name, d, br in REPOS:
        try:
            res = merge_to_main(name, d, br)
        except Exception as e:  # noqa: BLE001
            res = f"FALLO ({clean(e, 80)})"
        items.append((f"Repo {name} ({br} -> main)", res))
    try:
        src_sha = git("/root/work/teaspeak_v2-src", "rev-parse", "--short", "origin/main").stdout.strip()
        git("/root/work/teaspeak_v2", "fetch", "-q", "origin")
        tgt = git("/root/work/teaspeak_v2", "rev-parse", "origin/main").stdout.strip()
        if not items[1][1].startswith(("OK", "(dry)")):
            tgt = git("/root/work/teaspeak_v2", "rev-parse", "origin/build2-rendimiento").stdout.strip()
        if DRY and items[0][1].startswith("(dry)"):
            src_sha = git("/root/work/teaspeak_v2-src", "rev-parse", "--short", "origin/build2-rendimiento").stdout.strip()
        res, _ = publish_release(tgt, src_sha)
    except Exception as e:  # noqa: BLE001
        res = f"FALLO ({clean(e, 100)})"
    items.append((f"Release {TAG} (latest, mismos assets que build1)", res))
    return items


# ───────────────────────── resumen ─────────────────────────
def summary_items(outcome, run_txt, state):
    S = state.get("steps", {})
    wa_ok = run_txt.count("[wa] enviado ok=True")
    wa_all = wa_ok + run_txt.count("[wa] enviado ok=False") + run_txt.count("[wa] error") + run_txt.count("[wa] faltan")
    items = []
    reason = fail_reason(run_txt, state)
    pk = S.get("poke")
    if pk:
        items.append(("Aviso por poke (\"TsBot Alert\", 5 min antes)",
                      f"OK, {pk.get('poked')} clientes avisados (SPY excluidos)" if pk.get("poked", -1) >= 0
                      else "FALLO (error al hacer los pokes; el mantenimiento siguio)"))
    else:
        m = re.findall(r"pokeados: (-?\d+)", run_txt)
        items.append(("Aviso por poke (\"TsBot Alert\")", f"OK, {m[-1]} clientes avisados" if m else "NO enviado"))
    items.append(("WhatsApp de progreso al admin", f"{wa_ok}/{wa_all} enviados"))

    if outcome in ("no_ejecutado", "incompleto", "abortado"):
        if outcome == "no_ejecutado":
            items.append(("Mantenimiento", "NO SE EJECUTO (no hay ejecucion de hoy en /var/log/ts_maint_build2.log; "
                          "revisar el cron de las 09:57)"))
        elif outcome == "incompleto":
            items.append(("Mantenimiento", "INCOMPLETO (el log no tiene final: proceso cortado o aun en curso). "
                          "REVISAR TeaSpeak YA"))
        else:
            items.append(("Mantenimiento", f"ABORTADO antes de tocar TeaSpeak (no se reinicio nada): {reason}"))
        items.append(("Binario / hilos / TeaSpeak / bot", "sin cambios (sigue Build 1 con los hilos de antes)"
                      if outcome != "incompleto" else "estado desconocido"))
        items.append(("Repos y release", "OMITIDO (solo se publican si el mantenimiento sale OK)"))
        return items

    b = S.get("backups")
    items.append(("Backups (binario B1, config.yml, pg_dump de teaspeak)",
                  f"OK (dump {b.get('dump_mb')} MB)" if b else "no llegaron a completarse"))
    sw, th, up, bot, pc = S.get("swap"), S.get("threads_config"), S.get("teaspeak_up"), S.get("bot"), S.get("postchecks")

    if outcome in ("ok", "avisos"):
        res = (pc or {}).get("res", {})
        bad = (pc or {}).get("bad", [])
        items.append(("Cambio de binario B1 (ef2c6095) -> B2 (ae2a571c)",
                      f"OK (parada de B1 en {sw.get('stop_secs')} s)" if sw else "OK"))
        thr = f"{res.get('threads_before')} -> {res.get('threads_after')} hilos"
        items.append(("Hilos tuned en config.yml (ticking 2, command_execute 4, network_events 2, io_min 4, io_limit 4)",
                      ("REVISAR, " if "threads_after" in bad else "OK, ") + thr))
        items.append(("TeaSpeak arriba con Build 2",
                      ("REVISAR, " if "vservers" in bad else "OK, ") +
                      f"{res.get('vservers')}/{(up or {}).get('expected', 14)} vservers online, {(pc or {}).get('clients')} clientes"))
        stop_bad = [k for k in ("b1_stop_crash_dumps", "b1_stop_crash_in_log") if k in bad]
        items.append(("Parada limpia de Build 1 (fix T01)",
                      "OK (sin crash dump ni crash en el log)" if not stop_bad else
                      f"REVISAR (dumps={res.get('b1_stop_crash_dumps')}, crash en log={res.get('b1_stop_crash_in_log')})"))
        items.append(("Bot reiniciado", ("OK, " if "bots" not in bad else "REVISAR, ") +
                      f"{res.get('bots', '?')} instancias reconectadas"))
        hc_bad = [k for k in ("pg_log_errors", "ts_log_crashes_new", "crash_dumps_new", "sync_commit_off_line") if k in bad]
        items.append(("Health checks (errores PG, crashes y dumps nuevos, synchronous_commit off)",
                      "OK" if not hc_bad else "REVISAR: " + ", ".join(f"{k}={res.get(k)}" for k in hc_bad)))
        return items

    # rollback
    items.append(("Cambio de binario B1 -> B2", f"REVERTIDO a Build 1. Motivo: {reason}" if sw else
                  f"no llego a aplicarse. Motivo: {reason}"))
    items.append(("Hilos tuned en config.yml", "REVERTIDO (config.yml original restaurado)" if th else
                  "no llego a aplicarse (config.yml original restaurado)"))
    if outcome == "rollback_ok":
        items.append(("TeaSpeak tras el rollback", "OK, arriba con Build 1 y los hilos de antes"))
        items.append(("Bot reiniciado", "OK"))
    else:
        items.append(("TeaSpeak tras el rollback", "FALLO: INTERVENCION MANUAL YA (docs/VENTANA_20260929.md, rollback manual)"))
        items.append(("Bot", "estado desconocido"))
    items.append(("Repos y release", "OMITIDO (no se fusiona ni se publica porque se revirtio)"))
    return items


def live_status():
    """Comprobacion real en el momento del resumen (no depende del log)."""
    try:
        h = hashlib.md5()
        with open("/opt/teaspeak/TeaSpeakServer", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        m = h.hexdigest()
        name = {B1_MD5: "Build 1", B2_MD5: "Build 2"}.get(m, f"DESCONOCIDO {m[:8]}")
    except OSError as e:
        name = f"ilegible ({clean(e, 40)})"
    act = {u: subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True).stdout.strip()
           for u in ("teaspeak", "tsbot")}
    return f"binario en vivo {name}, teaspeak {act['teaspeak']}, tsbot {act['tsbot']}"


HEAD = {"ok":"TODO OK ✅", "avisos": "COMPLETADO CON AVISOS ⚠️ (revisar)", "rollback_ok": "REVERTIDO a Build 1 ❌",
        "rollback_fallido": "CRITICO: fallo el rollback 🚨", "abortado": "ABORTADO antes de empezar (no se toco nada) ⚠️",
        "no_ejecutado": "NO SE EJECUTO 🚨", "incompleto": "INCOMPLETO 🚨 (revisar ya)"}


def compose(title, items):
    return title + "\n\n" + "\n".join(f"- {k}: {v}" for k, v in items)


def main():
    log(f"=== post_build2 {'(DRY)' if DRY else '(REAL)'}{' publish-only' if args.publish_only else ''} ===")
    if args.publish_only:
        items = github_steps()
        text = compose("Hola, resultado de la publicacion de Build 2 (repos y release):", items)
        log("resumen:\n" + text)
        base.wa_send(text)
        return 0

    if not args.no_wait:
        t0 = time.time()
        while maint_running() and time.time() - t0 < 1800:
            time.sleep(20)
        if maint_running():
            log("ts_maint_build2.py sigue corriendo tras 30 min de espera")
    run_txt, state = read_run(), read_state()
    outcome = detect_outcome(run_txt)
    log(f"resultado del mantenimiento: {outcome} (state: {state.get('outcome', 'sin estado')})")
    items = summary_items(outcome, run_txt, state)
    items.append((f"Estado comprobado a las {datetime.now():%H:%M}", live_status()))
    if outcome == "ok":
        items += github_steps()
    elif outcome == "avisos":
        items.append(("Repos y release", "OMITIDO hasta revisar los avisos; despues: "
                      "/root/window-build2/post_build2.py --publish-only"))
    text = compose(f"Hola, resumen final del mantenimiento de TeaSpeak del {args.date[8:10]}-{args.date[5:7]} "
                   f"(Build 2 + hilos): {HEAD[outcome]}", items)
    log("resumen:\n" + text)
    ok = base.wa_send(text)
    log(f"=== post_build2 FIN (whatsapp={'dry' if DRY else ok}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
