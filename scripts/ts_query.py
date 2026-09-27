"""Cliente ServerQuery minimo (solo stdlib) para los scripts de mantenimiento de /opt/teaspeak/scripts.

Copia de lo imprescindible de `ts_ops.py` del dashboard (escape TS3, parseo de registros, send/use,
login) para que `ts_maint.py` y `ts_maint_bin.py` NO dependan de /opt/tsbot-dash ni de su venv
(auditoria F12). Compatible con el python3 del sistema (Debian 11: 3.9).
"""
from __future__ import annotations

import asyncio

_UNESC = {"\\\\": "\\", "\\/": "/", "\\s": " ", "\\p": "|", "\\a": "\a", "\\b": "\b",
          "\\f": "\f", "\\n": "\n", "\\r": "\r", "\\t": "\t", "\\v": "\v"}


def _unesc(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(_UNESC.get(s[i:i + 2], s[i + 1])); i += 2
        else:
            out.append(s[i]); i += 1
    return "".join(out)


def _esc(s: str) -> str:
    return (str(s).replace("\\", "\\\\").replace("/", "\\/").replace(" ", "\\s").replace("|", "\\p")
            .replace("\a", "\\a").replace("\b", "\\b").replace("\f", "\\f")
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace("\v", "\\v"))


def _parse(rec: str) -> dict:
    d = {}
    for part in rec.split(" "):
        if "=" in part:
            k, v = part.split("=", 1); d[k] = _unesc(v)
    return d


class TS:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    async def send(self, cmd: str, timeout: float = 25.0):
        self.w.write((cmd + "\n").encode()); await self.w.drain()
        buf = []
        while True:
            raw = await asyncio.wait_for(self.r.readline(), timeout)
            if not raw:
                return buf, {"id": "-1", "msg": "eof"}
            line = raw.decode("utf-8", "ignore").strip("\r\n")
            if line.startswith("error "):
                return buf, _parse(line[6:])
            if line:
                buf.append(line)

    async def use(self, sid):
        await self.send(f"use {sid}")

    async def close(self):
        try:
            self.w.write(b"quit\n"); await self.w.drain()
        except Exception:  # noqa: BLE001
            pass
        self.w.close()


async def connect(creds: dict) -> TS:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(creds["ts_address"], creds["ts_port"], limit=16 * 1024 * 1024), 8)
    ts = TS(reader, writer)
    await asyncio.wait_for(reader.readline(), 6)  # banner
    try:
        await asyncio.wait_for(reader.readline(), 0.5)
    except asyncio.TimeoutError:
        pass
    _, e = await ts.send(f'login {_esc(creds["ts_query_user"])} {_esc(creds["ts_query_pass"])}')
    if e.get("id") not in ("0", None):
        raise RuntimeError(f"login ServerQuery: {e.get('msg')}")
    return ts
