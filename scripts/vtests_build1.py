#!/usr/bin/env python3
"""Pruebas V de Build 1 (docs/BUILD1.md §5) en un vserver DESECHABLE `zz-e2e-life`, creado y borrado con el ciclo de
vida de ops (tsbotops.lifecycle), con `global_config.ops_lifecycle_enabled` a 1 solo mientras se crea/borra y de
vuelta a 0 siempre (finally).

SOLO LECTURA contra los demas vservers: todo lo que escribe va a `use <sid de zz-e2e-life>`. Lo unico que toca el
template es lo mismo que ya hace el alta de ops: `serversnapshotcreate` (una exportacion, de solo lectura) — V2 lo
repite para contar los clientes exportados.

Pruebas automaticas:
  V1 (T03)  3x servergroupadd -> ids > MAX(groupid) previo y distintos; 2 altas en paralelo -> ids distintos;
            secuencia >= MAX(groupid); 0 `groups_pkey` en el log de PG.
  V2 (T02)  snapshot del template con `client_id=` entre begin_clients/end_clients (si su clientdblist > 0);
            `bantriggerlist` sin limite en zz; 0 `LIMIT must not be negative`.
  V3 (T04)  serversnapshotcreate + serversnapshotdeploy de zz sobre zz: mismos grupos de servidor y de canal y los
            mismos permisos (valor, negated, skip) por nombre; 0 `more expressions than target columns`.
  V4 (T08)  sesiones query A y B en zz: A hace altas/renombres/copias/permisos/bajas de grupos de servidor y canal;
            B no recibe nada que no pidio y su `whoami` sale limpio; A tampoco recibe listas espontaneas.
  V5 (T06)  invitado query (querycreate en zz) sin b_channel_ignore_view_power frente a un cliente X en un canal
            oculto: clientgetuidfromclid / clientinfo / clientgetids / clientlist / clientfind no lo revelan;
            serveradmin si lo ve. X = una 2a sesion query movida al canal oculto; si TeaSpeak no lo permite o
            no se puede demostrar que el invitado no tenga el permiso, INCONCLUSO (-> subcomando `voice`).
  V6 (T07)  canal oculto (i_channel_needed_view_power 75) con un hijo: channelinfo -> error y channelfind no lo
            lista para el invitado; serveradmin lo ve todo.
  V8 (T09/T10, parte normal) 10 subidas y bajadas por el puerto de ficheros (contenido identico), claves 'raw…'
            distintas; subida y bajada de un icono.
Manual (V9): cliente TS3 como Server Admin en zz (clave de privilegio en un fichero 600) -> editor de permisos de
canal y de grupos. Subcomando `voice`: repite V5 con un cliente de VOZ real (el tuyo) en el canal oculto.

Uso (como root en prod, con el entorno de ops):
  run_vtests.sh --check        dry-run: importa ops, credenciales, template, puerto libre, login query, log de PG
  run_vtests.sh run            crea zz-e2e-life, pruebas automaticas, lo deja arriba para V9 (salvo --cleanup)
  run_vtests.sh voice          V5 con un cliente de voz conectado a zz (lo mueve al canal oculto)
  run_vtests.sh cleanup        borra zz-e2e-life (flag 1 -> borrar -> 0) y el fichero de la clave
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

NAME = "zz-e2e-life"
VDIR = os.environ.get("VTESTS_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(VDIR, "state.json")
TOKEN_FILE = os.path.join(VDIR, "zz_server_admin_token.txt")
PG_LOG = "/var/log/postgresql/postgresql-13-main.log"
BUILD1_MD5 = "ef2c609533996e81044b33c7e3c09d71"   # Build 1 + T20c (28-sep 05:12)
LIVE = "/opt/teaspeak/TeaSpeakServer"
FLAG = "ops_lifecycle_enabled"
RESULTS: list[tuple[str, str, str]] = []


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def res(test, status, detail=""):
    RESULTS.append((test, status, str(detail)[:300]))
    log(f"{status:12} {test}: {detail}")


def psql(sql, db="tsbot"):
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-X", "-tA", "-d", db, "-c", sql],
                       capture_output=True, text=True, cwd="/tmp")
    return r.stdout.strip()


def pg_errs(since, pat):
    r = subprocess.run(["bash", "-c", f"awk -v s='{since}' 'substr($0,1,19) >= s' {PG_LOG} | grep -ciE '{pat}' || true"],
                       capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return -1


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# ───────────── ServerQuery "en crudo": guarda TODO lo que llega, incluido lo no pedido ─────────────
def esc(s: str) -> str:
    return (str(s).replace("\\", "\\\\").replace("/", "\\/").replace(" ", "\\s").replace("|", "\\p")
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


def unesc(s: str) -> str:
    out, i = [], 0
    m = {"\\": "\\", "/": "/", "s": " ", "p": "|", "n": "\n", "r": "\r", "t": "\t"}
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(m.get(s[i + 1], s[i + 1])); i += 2
        else:
            out.append(s[i]); i += 1
    return "".join(out)


def parse(line: str) -> dict:
    d = {}
    for tok in line.split(" "):
        if "=" in tok:
            k, v = tok.split("=", 1); d[k] = unesc(v)
        elif tok:
            d[tok] = ""
    return d


def recs(lines: list[str]) -> list[dict]:
    return [parse(x) for x in lines[0].split("|")] if lines and lines[0] else []


def ftrec(lines: list[str]) -> dict:
    """Respuesta de ftinitupload/ftinitdownload: TeaSpeak intercala antes notifyfiletransfer* de transferencias
    anteriores, asi que se toma la primera linea que trae ftkey (no la linea 0)."""
    for ln in lines:
        for d in (parse(x) for x in ln.split("|")):
            if "ftkey" in d:
                return d
    return {}


class Q:
    def __init__(self, r, w, tag):
        self.r, self.w, self.tag = r, w, tag
        self.unsolicited: list[str] = []

    @classmethod
    async def open(cls, user, pw, tag="q"):
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 10101, limit=32 << 20), 8)
        q = cls(r, w, tag)
        await asyncio.wait_for(r.readline(), 6)
        try:
            await asyncio.wait_for(r.readline(), 0.5)
        except asyncio.TimeoutError:
            pass
        _, e = await q.send(f"login {esc(user)} {esc(pw)}")
        if e.get("id") != "0":
            raise RuntimeError(f"{tag}: login fallo ({e.get('id')} {e.get('msg')})")
        return q

    async def drain(self, wait=1.5) -> list[str]:
        """Lee lo que haya llegado sin pedirlo (espera `wait` s)."""
        got = []
        while True:
            try:
                raw = await asyncio.wait_for(self.r.readline(), wait)
            except asyncio.TimeoutError:
                break
            if not raw:
                break
            ln = raw.decode("utf-8", "ignore").strip("\r\n")
            if ln:
                got.append(ln)
            wait = 0.3
        self.unsolicited += got
        return got

    async def send(self, cmd, timeout=60.0):
        self.w.write((cmd + "\n").encode()); await self.w.drain()
        buf = []
        while True:
            raw = await asyncio.wait_for(self.r.readline(), timeout)
            if not raw:
                return buf, {"id": "-1", "msg": "eof"}
            ln = raw.decode("utf-8", "ignore").strip("\r\n")
            if ln.startswith("error "):
                return buf, parse(ln[6:])
            if ln:
                buf.append(ln)

    async def must(self, cmd, timeout=60.0):
        b, e = await self.send(cmd, timeout)
        if e.get("id") != "0":
            raise RuntimeError(f"{self.tag}: '{cmd.split(' ')[0]}' -> {e.get('id')} {e.get('msg')}")
        return b

    async def close(self):
        try:
            self.w.write(b"quit\n"); await self.w.drain()
        except Exception:  # noqa: BLE001
            pass
        self.w.close()


def creds():
    row = psql("SELECT ts_query_user||'|'||ts_query_pass FROM instances ORDER BY id LIMIT 1").split("|", 1)
    return {"ts_address": "127.0.0.1", "ts_port": 10101, "ts_query_user": row[0], "ts_query_pass": row[1]}


def template_sid():
    v = psql("SELECT value FROM global_config WHERE key='dash_template_sid'")
    return int(v) if v.isdigit() else 26


def flag_get():
    return psql(f"SELECT value FROM global_config WHERE key='{FLAG}'") or "(sin fila = 0)"


def flag_set(v: str):
    psql(f"INSERT INTO global_config(key,value) VALUES ('{FLAG}','{v}') "
         f"ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value")
    log(f"{FLAG} = {flag_get()}")


async def serverlist(q):
    return {int(d["virtualserver_id"]): d for d in recs(await q.must("serverlist"))}


def save_state(d):
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except OSError:
        return {}


# ───────────── estado comparable de grupos (por nombre) ─────────────
async def groups_state(q, sid):
    await q.must(f"use {sid}")
    sg = {d["name"]: d for d in recs(await q.must("servergrouplist")) if d.get("type") == "1"}
    cg = {d["name"]: d for d in recs(await q.must("channelgrouplist")) if d.get("type") == "1"}
    out = {"sg": {}, "cg": {}}
    for n, d in sg.items():
        b, e = await q.send(f"servergrouppermlist sgid={d['sgid']} -permsid")
        out["sg"][n] = sorted((p.get("permsid"), p.get("permvalue"), p.get("permnegated"), p.get("permskip"))
                              for p in recs(b)) if e.get("id") == "0" else []
    for n, d in cg.items():
        b, e = await q.send(f"channelgrouppermlist cgid={d['cgid']} -permsid")
        out["cg"][n] = sorted((p.get("permsid"), p.get("permvalue")) for p in recs(b)) if e.get("id") == "0" else []
    return out


def diff_states(a, b):
    probs = []
    for kind in ("sg", "cg"):
        miss = set(a[kind]) - set(b[kind])
        extra = set(b[kind]) - set(a[kind])
        if miss:
            probs.append(f"{kind} que faltan: {sorted(miss)[:5]}")
        if extra:
            probs.append(f"{kind} de mas: {sorted(extra)[:5]}")
        for n in set(a[kind]) & set(b[kind]):
            if a[kind][n] != b[kind][n]:
                probs.append(f"{kind} '{n}': permisos distintos ({len(a[kind][n])} vs {len(b[kind][n])})")
    return probs


# ───────────── pruebas ─────────────
async def v1_v3(c, sid, since):
    q = await Q.open(c["ts_query_user"], c["ts_query_pass"], "v1")
    try:
        await q.must(f"use {sid}")
        seq0, max0 = psql("SELECT s.last_value||'|'||(SELECT max(groupid) FROM groups) FROM pg_sequences s WHERE "
                          "s.schemaname||'.'||s.sequencename = pg_get_serial_sequence('groups','groupid')", "teaspeak").split("|")
        ids = []
        for i in range(3):
            b = await q.must(f"servergroupadd name=zz-v1-{i} type=1")
            ids.append(int(recs(b)[0]["sgid"]))
        ok = all(x > int(max0) for x in ids) and len(set(ids)) == 3
        res("V1 ids secuencia", "PASS" if ok else "FAIL", f"max_previo={max0} seq_previa={seq0} nuevos={ids}")
        q2 = await Q.open(c["ts_query_user"], c["ts_query_pass"], "v1b")
        await q2.must(f"use {sid}")
        ra, rb = await asyncio.gather(q.send("servergroupadd name=zz-v1-par-a type=1"),
                                      q2.send("servergroupadd name=zz-v1-par-b type=1"))
        pa, pb = recs(ra[0]), recs(rb[0])
        pid = [int(x[0]["sgid"]) for x in (pa, pb) if x and "sgid" in x[0]]
        res("V1 altas en paralelo", "PASS" if len(pid) == 2 and pid[0] != pid[1] else "FAIL", f"sgids={pid}")
        await q2.close()
        seq_ok = psql("SELECT s.last_value >= (SELECT max(groupid) FROM groups) FROM pg_sequences s WHERE "
                      "s.schemaname||'.'||s.sequencename = pg_get_serial_sequence('groups','groupid')", "teaspeak")
        res("V1 secuencia >= MAX(groupid)", "PASS" if seq_ok == "t" else "FAIL", seq_ok)

        before = await groups_state(q, sid)
        await q.must(f"use {sid}")
        snap = (await q.must("serversnapshotcreate", 180))[0]
        await q.must(f"serversnapshotdeploy {snap}", 300)
        await asyncio.sleep(3)
        after = await groups_state(q, sid)
        probs = diff_states(before, after)
        res("V3 deploy: grupos y permisos (valor/negated/skip)", "PASS" if not probs else "FAIL",
            f"sg={len(before['sg'])} cg={len(before['cg'])} " + "; ".join(probs[:4]))
        g = pg_errs(since, "groups_pkey")
        m = pg_errs(since, "more expressions than target")
        res("V1/V3 log PG", "PASS" if g == 0 and m == 0 else "FAIL", f"groups_pkey={g} more_expressions={m}")
    finally:
        await q.close()


async def v2(c, sid, tpl, since):
    q = await Q.open(c["ts_query_user"], c["ts_query_pass"], "v2")
    try:
        await q.must(f"use {tpl}")                      # solo lectura en el template (exportacion)
        b, e = await q.send("clientdblist start=0 duration=1 -count")
        total = int(recs(b)[0].get("count", 0)) if e.get("id") == "0" and b else 0
        # sin version=, TeaSpeak devuelve el snapshot comprimido (zstd+base64): version=2 lo da en claro
        snap = "\n".join(await q.must("serversnapshotcreate version=2", 180))
        m = re.search(r"begin_clients(.*?)end_clients", snap, re.S)
        n = len(re.findall(r"client_id=", m.group(1))) if m else 0
        st = "PASS" if (n > 0 if total > 0 else True) else "FAIL"
        res("V2 snapshot exporta clientes", st, f"template sid={tpl}: clientdblist={total} client_id_en_snapshot={n}")
        await q.must(f"use {sid}")
        await q.must("banadd ip=192.0.2.123 banreason=zz-e2e time=600")
        # banadd de TeaSpeak solo responde ok (sin banid) y banlist hace JOIN con clients_server del invocador
        # (serveradmin no tiene fila en un vserver nuevo): el banid se lee de la BD
        banid = psql(f"SELECT max(banid) FROM bannedclients WHERE serverid={sid} AND ip='192.0.2.123'", "teaspeak")
        b, e = await q.send(f"bantriggerlist banid={banid}")
        await q.send(f"bandel banid={banid}")
        lim = pg_errs(since, "LIMIT must not be negative")
        res("V2 bantriggerlist sin limite", "PASS" if e.get("id") in ("0", "1281") and lim == 0 else "FAIL",
            f"respuesta={e.get('id')} {e.get('msg')} LIMIT_negativo_en_PG={lim}")
    finally:
        await q.close()


async def v4(c, sid):
    a = await Q.open(c["ts_query_user"], c["ts_query_pass"], "A")
    b = await Q.open(c["ts_query_user"], c["ts_query_pass"], "B")
    try:
        await a.must(f"use {sid}"); await b.must(f"use {sid}")
        await b.drain(1.0); b.unsolicited.clear()
        extra_a = []
        r = await a.must("servergroupadd name=zz-v4 type=1"); extra_a += r[1:]
        sg = recs(r)[0]["sgid"]
        for cmd in (f"servergrouprename sgid={sg} name=zz-v4-ren",
                    f"servergroupaddperm sgid={sg} permsid=i_client_talk_power permvalue=10 permnegated=0 permskip=0"):
            extra_a += await a.must(cmd)
        r = await a.must(f"servergroupcopy ssgid={sg} tsgid=0 name=zz-v4-copy type=1"); extra_a += r[1:]
        cp = recs(r)[0].get("sgid")
        for g in (sg, cp):
            if g:
                extra_a += await a.must(f"servergroupdel sgid={g} force=1")
        r = await a.must("channelgroupadd name=zz-v4-cg type=1"); extra_a += r[1:]
        cg = recs(r)[0]["cgid"]
        extra_a += await a.must(f"channelgrouprename cgid={cg} name=zz-v4-cg-ren")
        extra_a += await a.must(f"channelgroupaddperm cgid={cg} permsid=i_channel_needed_join_power permvalue=5")
        extra_a += await a.must(f"channelgroupdel cgid={cg} force=1")
        got = await b.drain(2.0)
        wb, e = await b.send("whoami")
        clean = e.get("id") == "0" and len(wb) == 1 and "virtualserver_id=" in wb[0]
        res("V4 B sin notificaciones espontaneas", "PASS" if not got else "FAIL", f"{len(got)} lineas no pedidas: "
            + "; ".join(x[:60] for x in got[:3]))
        res("V4 whoami de B limpio", "PASS" if clean else "FAIL", f"lineas={len(wb)}")
        res("V4 A sin listas espontaneas", "PASS" if not extra_a else "FAIL", f"{len(extra_a)} lineas de mas")
    finally:
        await a.close(); await b.close()


async def make_hidden(q, sid):
    await q.must(f"use {sid}")
    for d in recs(await q.must("channellist")):
        if d.get("channel_name") in ("zz-hidden", "zz-files"):
            return None
    h = recs(await q.must("channelcreate channel_name=zz-hidden channel_flag_permanent=1"))[0]["cid"]
    ch = recs(await q.must(f"channelcreate channel_name=zz-hidden-child cpid={h} channel_flag_permanent=1"))[0]["cid"]
    await q.must(f"channeladdperm cid={h} permsid=i_channel_needed_view_power permvalue=75")
    vis = recs(await q.must("channelcreate channel_name=zz-files channel_flag_permanent=1"))[0]["cid"]
    return {"hidden": int(h), "child": int(ch), "files": int(vis)}


async def guest_session(adm, sid):
    """Cuenta query de invitado en zz (querycreate de TeaSpeak). Devuelve (Q, detalle) o (None, motivo)."""
    b, e = await adm.send(f"querycreate client_login_name=zz_e2e_guest server_id={sid}")
    if e.get("id") != "0":
        return None, f"querycreate: {e.get('id')} {e.get('msg')}"
    pw = recs(b)[0].get("client_login_password")
    g = await Q.open("zz_e2e_guest", pw, "guest")
    _, e = await g.send(f"use {sid}")
    if e.get("id") != "0":
        return None, f"invitado no puede usar sid {sid}: {e.get('msg')}"
    return g, ""


async def guest_has_ignore_view(g):
    b, e = await g.send("permget permsid=b_channel_ignore_view_power")
    if e.get("id") != "0":
        return None if e.get("id") not in ("1281", "2568") else False
    v = recs(b)[0].get("permvalue") if b else None
    return v not in (None, "0")


async def v5_v6(c, sid, ch, x_clid=None):
    adm = await Q.open(c["ts_query_user"], c["ts_query_pass"], "adm")
    g = xq = None
    try:
        await adm.must(f"use {sid}")
        g, why = await guest_session(adm, sid)
        if not g:
            res("V5/V6 invitado", "INCONCLUSO", why); return
        iv = await guest_has_ignore_view(g)
        if iv is not False:
            res("V5/V6 invitado sin b_channel_ignore_view_power", "INCONCLUSO", f"permget -> {iv}")
        # V6
        ctl = (await g.send(f"channelinfo cid={ch['files']}"))[1]
        h = (await g.send(f"channelinfo cid={ch['hidden']}"))[1]
        k = (await g.send(f"channelinfo cid={ch['child']}"))[1]
        fb, fe = await g.send("channelfind pattern=zz-hidden")
        found = [d.get("channel_name") for d in recs(fb)] if fe.get("id") == "0" else []
        ab, _ = await adm.send("channelfind pattern=zz-hidden")
        adm_found = len(recs(ab))
        if ctl.get("id") != "0":
            res("V6 control (canal visible)", "INCONCLUSO", f"el invitado no puede ni ver un canal visible: {ctl.get('msg')}")
        ok6 = h.get("id") != "0" and k.get("id") != "0" and not found and adm_found >= 2
        res("V6 canal oculto e hijo", "PASS" if ok6 and iv is False and ctl.get("id") == "0" else
            ("FAIL" if not ok6 else "INCONCLUSO"),
            f"channelinfo oculto={h.get('id')} hijo={k.get('id')} channelfind_invitado={found} serveradmin={adm_found}")
        # V5: X en el canal oculto
        if x_clid is None:
            xq = await Q.open(c["ts_query_user"], c["ts_query_pass"], "X")
            await xq.must(f"use {sid}")
            me = parse((await xq.must("whoami"))[0])
            x_clid = int(me["client_id"])
            _, e = await xq.send(f"clientmove clid={x_clid} cid={ch['hidden']}")
            if e.get("id") != "0":
                res("V5 X (query) en canal oculto", "INCONCLUSO", f"clientmove: {e.get('msg')} -> usar `voice`")
                return
        info = recs((await adm.send(f"clientinfo clid={x_clid}"))[0])
        xuid = info[0].get("client_unique_identifier") if info else None
        if not xuid or info[0].get("cid") not in (str(ch["hidden"]), None):
            res("V5 X en canal oculto (visto por serveradmin)", "INCONCLUSO", f"clientinfo adm={bool(info)} cid={info[0].get('cid') if info else '-'}")
        r1 = (await g.send(f"clientgetuidfromclid clid={x_clid}"))
        r2 = (await g.send(f"clientinfo clid={x_clid}"))
        r3 = (await g.send(f"clientgetids cluid={esc(xuid or 'x')}"))
        r4 = recs((await g.send("clientlist"))[0])
        r5 = recs((await g.send("clientfind pattern="))[0])
        leak = [n for n, r in (("clientgetuidfromclid", r1), ("clientinfo", r2), ("clientgetids", r3))
                if r[1].get("id") == "0" and r[0]]
        leak += ["clientlist"] if any(d.get("clid") == str(x_clid) for d in r4) else []
        leak += ["clientfind"] if any(d.get("clid") == str(x_clid) for d in r5) else []
        adm_ok = bool(xuid)
        st = "FAIL" if leak else ("PASS" if adm_ok and iv is False else "INCONCLUSO")
        res("V5 cliente en canal oculto no se revela", st, f"fugas={leak} serveradmin_lo_ve={adm_ok}")
    finally:
        for s in (g, xq, adm):
            if s:
                await s.close()


async def ft_io(port, key: str, payload: bytes | None, size: int = 0) -> bytes:
    r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(port)), 8)
    w.write(key.encode("latin-1"))
    if payload is not None:
        w.write(payload); await w.drain()
        await asyncio.sleep(0.5); w.close()
        return b""
    await w.drain()
    data = await asyncio.wait_for(r.readexactly(size), 20)
    w.close()
    return data


async def v8(c, sid, ch):
    q = await Q.open(c["ts_query_user"], c["ts_query_pass"], "v8")
    keys, bad = [], []
    try:
        await q.must(f"use {sid}")
        for i in range(10):
            data = os.urandom(2048 + i)
            b = await q.must(f"ftinitupload clientftfid={100 + i} name=\\/zz_e2e_{i}.bin cid={ch['files']} cpw= "
                             f"size={len(data)} overwrite=1 resume=0")
            u = ftrec(b)
            keys.append(u["ftkey"])
            await ft_io(u["port"], u["ftkey"], data)
            await asyncio.sleep(0.3)
            b = await q.must(f"ftinitdownload clientftfid={200 + i} name=\\/zz_e2e_{i}.bin cid={ch['files']} cpw= seekpos=0")
            d = ftrec(b)
            got = await ft_io(d["port"], d["ftkey"], None, int(d["size"]))
            keys.append(d["ftkey"])
            if got != data:
                bad.append(i)
        res("V8 10 subidas/bajadas", "PASS" if not bad else "FAIL", f"fallidas={bad}")
        distinct = len(set(keys)) == len(keys)
        form = all(k.startswith("raw") and len(k) == len(keys[0]) for k in keys)
        res("V8 claves distintas 'raw…'", "PASS" if distinct and form else "FAIL",
            f"{len(keys)} claves, distintas={distinct}, formato_ok={form}, longitud={len(keys[0])}")
        png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                            "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
        crc = int.from_bytes(hashlib.md5(png).digest()[:4], "big") & 0x7FFFFFFF
        b = await q.must(f"ftinitupload clientftfid=300 name=\\/icon_{crc} cid=0 cpw= size={len(png)} overwrite=1 resume=0")
        u = ftrec(b)
        await ft_io(u["port"], u["ftkey"], png)
        await asyncio.sleep(0.3)
        b = await q.must(f"ftinitdownload clientftfid=301 name=\\/icon_{crc} cid=0 cpw= seekpos=0")
        d = ftrec(b)
        got = await ft_io(d["port"], d["ftkey"], None, int(d["size"]))
        res("V8 icono subida/bajada", "PASS" if got == png else "FAIL", f"icon_{crc} {len(got)} bytes")
    except Exception as ex:  # noqa: BLE001
        res("V8", "FAIL", f"excepcion: {type(ex).__name__}: {str(ex)[:150]}")
    finally:
        await q.close()


# ───────────── ciclo de vida (ops) ─────────────
async def create_zz(c):
    from tsbotops import lifecycle
    tpl = template_sid()
    port = await lifecycle.next_free_port(c)
    old = flag_get()
    flag_set("1")
    try:
        out = await lifecycle.clone_create(c, tpl, NAME, port, 10, progress=lambda s: log(f"  alta: {s}"))
    finally:
        flag_set("0")
    log(f"zz-e2e-life creado: sid={out['sid']} puerto={port} (flag previo={old}); verificacion alta: "
        f"faltan_sg={out.get('missing_server_groups')} diffs_sg={out.get('server_perm_diffs')}")
    return out["sid"], port, tpl


async def delete_zz(c, sid):
    from tsbotops import lifecycle
    q = await Q.open(c["ts_query_user"], c["ts_query_pass"], "del")
    try:
        sl = await serverlist(q)
    finally:
        await q.close()
    if sid not in sl or sl[sid].get("virtualserver_name") != NAME:
        log(f"cleanup: el sid {sid} no existe o no se llama {NAME}: NO se borra nada")
        return False
    flag_set("1")
    try:
        r = await lifecycle.delete_vserver(c, sid)
    finally:
        flag_set("0")
    log(f"cleanup: borrado sid={sid}: {r}")
    return r.get("ok")


async def server_admin_token(c, sid):
    q = await Q.open(c["ts_query_user"], c["ts_query_pass"], "tok")
    try:
        await q.must(f"use {sid}")
        sg = next((d["sgid"] for d in recs(await q.must("servergrouplist"))
                   if d.get("name") == "Server Admin" and d.get("type") == "1"), None)
        if not sg:
            return None
        b = await q.must(f"privilegekeyadd tokentype=0 tokenid1={sg} tokenid2=0 tokendescription=zz-e2e-V9")
        tok = recs(b)[0].get("token")
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok + "\n")
        return TOKEN_FILE
    finally:
        await q.close()


def summary():
    log("──────── RESUMEN ────────")
    for t, s, d in RESULTS:
        print(f"  {s:11} {t}  | {d}")
    n = {s: sum(1 for _, x, _ in RESULTS if x == s) for s in ("PASS", "FAIL", "INCONCLUSO")}
    log(f"PASS={n['PASS']} FAIL={n['FAIL']} INCONCLUSO={n['INCONCLUSO']}")
    return 1 if n["FAIL"] else 0


async def main():
    cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), "run")
    check = "--check" in sys.argv
    c = creds()
    if check:
        from tsbotops import lifecycle  # noqa: F401  (import de ops con su entorno)
        q = await Q.open(c["ts_query_user"], c["ts_query_pass"], "check")
        sl = await serverlist(q)
        who = parse((await q.must("whoami"))[0])
        await q.close()
        tpl = template_sid()
        port = await lifecycle.next_free_port(c)
        zz = [s for s, d in sl.items() if d.get("virtualserver_name") == NAME]
        log(f"[check] ops importado; login query OK ({who.get('client_login_name')}); vservers={len(sl)}; "
            f"template sid={tpl} ({sl.get(tpl, {}).get('virtualserver_name')}); puerto libre={port}; "
            f"{FLAG}={flag_get()}; zz-e2e-life existente={zz or 'no'}")
        log(f"[check] binario vivo md5={md5(LIVE)[:8]} (el modo run exige Build 1 {BUILD1_MD5[:8]})")
        log(f"[check] log de PG legible: {os.access(PG_LOG, os.R_OK)}; estado previo: {load_state() or 'ninguno'}")
        now = datetime.now().strftime("%H:%M")
        log(f"[check] hora {now}: el modo run se niega entre 09:45 y 10:30 salvo --allow-window")
        log("[check] OK. No se creo ni se toco nada.")
        return 0

    if cmd == "cleanup":
        st = load_state()
        if not st.get("sid"):
            log("cleanup: no hay estado (state.json); nada que borrar"); return 0
        ok = await delete_zz(c, int(st["sid"]))
        if ok:
            for p in (STATE, TOKEN_FILE):
                if os.path.exists(p):
                    os.remove(p)
        return 0 if ok else 1

    if md5(LIVE) != BUILD1_MD5 and "--any-binary" not in sys.argv:
        log("ABORTADO: el binario vivo no es Build 1 (usa --any-binary solo para ensayar)"); return 2
    hm = datetime.now().strftime("%H:%M")
    if "09:45" <= hm <= "10:30" and "--allow-window" not in sys.argv:
        log("ABORTADO: franja 09:45-10:30 (docs/BUILD1.md); relanzar despues de las 10:30"); return 2

    if cmd == "voice":
        st = load_state()
        sid, ch = int(st["sid"]), st["channels"]
        q = await Q.open(c["ts_query_user"], c["ts_query_pass"], "voice")
        await q.must(f"use {sid}")
        voice = [d for d in recs(await q.must("clientlist")) if d.get("client_type") == "0"]
        if not voice:
            await q.close(); log("voice: no hay ningun cliente de voz en zz-e2e-life"); return 2
        x = int(voice[0]["clid"])
        await q.must(f"clientmove clid={x} cid={ch['hidden']}")
        await q.close()
        await v5_v6(c, sid, ch, x_clid=x)
        return summary()

    # run
    st = load_state()
    if st.get("sid"):
        log(f"ABORTADO: ya hay un zz-e2e-life de una ejecucion anterior (sid={st['sid']}); `cleanup` primero"); return 2
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid, port, tpl = await create_zz(c)
    save_state({"sid": sid, "port": port, "since": since})
    try:
        adm = await Q.open(c["ts_query_user"], c["ts_query_pass"], "setup")
        ch = await make_hidden(adm, sid)
        await adm.close()
        save_state({"sid": sid, "port": port, "since": since, "channels": ch})
        for name, coro in (("V1/V3", v1_v3(c, sid, since)), ("V2", v2(c, sid, tpl, since)), ("V4", v4(c, sid))):
            try:
                await coro
            except Exception as ex:  # noqa: BLE001
                res(name, "FAIL", f"excepcion: {type(ex).__name__}: {str(ex)[:160]}")
        # tras el deploy de V3 los cid pueden cambiar: se recrean los canales si hace falta
        adm = await Q.open(c["ts_query_user"], c["ts_query_pass"], "setup2")
        await adm.must(f"use {sid}")
        chl = {d.get("channel_name"): int(d["cid"]) for d in recs(await adm.must("channellist"))}
        await adm.close()
        ch = {"hidden": chl.get("zz-hidden"), "child": chl.get("zz-hidden-child"), "files": chl.get("zz-files")}
        save_state({"sid": sid, "port": port, "since": since, "channels": ch})
        for name, coro in (("V5/V6", v5_v6(c, sid, ch)), ("V8", v8(c, sid, ch))):
            try:
                await coro
            except Exception as ex:  # noqa: BLE001
                res(name, "FAIL", f"excepcion: {type(ex).__name__}: {str(ex)[:160]}")
        tf = await server_admin_token(c, sid)
        log(f"V9 (manual): conecta un cliente TS3 a 23.26.121.40:{port} y usa la clave de privilegio de "
            f"'Server Admin' guardada en {tf} (600). Luego `run_vtests.sh voice` y `run_vtests.sh cleanup`.")
    finally:
        if "--cleanup" in sys.argv:
            if await delete_zz(c, sid):
                for p in (STATE, TOKEN_FILE):
                    if os.path.exists(p):
                        os.remove(p)
        log(f"{FLAG} final = {flag_get()}")
    return summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
