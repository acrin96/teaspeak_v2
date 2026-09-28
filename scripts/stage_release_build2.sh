#!/bin/bash
# Prepara (SIN publicar) los assets de la release v1.4.21-beta-3-build2 de acrin96/teaspeak_v2:
#   - mismo bundle que v1.4.21-beta-3-build1 (mismos nombres de asset, que es lo que baja install.sh desde
#     releases/latest) con el binario sustituido por Build 2 (md5 ae2a571c) y la seccion threads de
#     config.template.yml igual a la de prod tras la ventana del 29-sep (hilos tuned);
#   - geoloc sin cambios; SHA256SUMS regenerado.
# Deja los ficheros en /root/window-build2/release/. Los publica post_build2.py (solo si la ventana sale OK).
# Solo lee de GitHub y escribe en /root/work/rel_build2 y /root/window-build2/release; no toca produccion.
set -euo pipefail
B1_TAG=v1.4.21-beta-3-build1
OUT=/root/window-build2/release
WORK=/root/work/rel_build2
B1_MD5=ef2c609533996e81044b33c7e3c09d71
B2=/root/build-out/build2/TeaSpeakServer.build2
B2_MD5=ae2a571c74fa997f84d215104af9f169
BUNDLE=teaspeak_v2_1.4.21-beta-3_linux_amd64.tar.gz
GEO=teaspeak_v2_geoloc.tar.gz
PY=/opt/tsbot-dash/venv/bin/python

mkdir -p "$WORK/b1" "$OUT"
for a in "$BUNDLE" "$GEO" SHA256SUMS; do
    curl -fsSL -o "$WORK/b1/$a" "https://github.com/acrin96/teaspeak_v2/releases/download/$B1_TAG/$a"
done
(cd "$WORK/b1" && sha256sum -c SHA256SUMS)

[ "$(md5sum < "$B2" | cut -d' ' -f1)" = "$B2_MD5" ] || { echo "binario Build 2 con md5 inesperado"; exit 1; }
rm -rf "$WORK/x"
mkdir "$WORK/x"
tar -xzf "$WORK/b1/$BUNDLE" -C "$WORK/x"
T="$WORK/x/teaspeak_v2_bundle"
[ "$(md5sum < "$T/TeaSpeakServer" | cut -d' ' -f1)" = "$B1_MD5" ] || { echo "el bundle de build1 no trae Build 1"; exit 1; }
install -m 755 "$B2" "$T/TeaSpeakServer"

# threads de config.template.yml = los de prod tras la ventana (tuned)
"$PY" - "$T/config.template.yml" <<'EOF'
import copy, sys, yaml
path = sys.argv[1]
want = {("", "ticking"): 2, ("", "command_execute"): 4, ("", "network_events"): 2,
        ("voice", "events_per_server"): 2, ("voice", "execute_per_server"): 2, ("voice", "execute_limit"): 64,
        ("voice", "io_min"): 4, ("voice", "io_per_server"): 2, ("voice", "io_limit"): 4,
        ("voice", "bind_io_thread_to_kernel_thread"): 1}
src = open(path, encoding="utf-8").read()
lines = src.split("\n")
found, in_t, sub = {}, False, ""
for i, line in enumerate(lines):
    s = line.lstrip(); ind = len(line) - len(s)
    if not s or s.startswith("#"):
        continue
    if ind == 0:
        in_t, sub = s.startswith("threads:"), ""
        continue
    if not in_t:
        continue
    key, sep, rest = s.partition(":")
    if not sep:
        continue
    if ind == 2:
        if rest.strip() == "":
            sub = key.strip(); continue
        sub, k = "", ("", key.strip())
    elif ind == 4:
        k = (sub, key.strip())
    else:
        continue
    if k in want:
        assert k not in found, f"duplicada {k}"
        found[k] = rest.strip()
        lines[i] = " " * ind + f"{k[1]}: {want[k]}"
missing = [k for k in want if k not in found]
assert not missing, f"faltan {missing}"
new = "\n".join(lines)
o, n = yaml.safe_load(src), yaml.safe_load(new)
for (s_, k), v in want.items():
    assert (n["threads"][s_] if s_ else n["threads"])[k] == v, (s_, k)
def strip(d):
    d = copy.deepcopy(d)
    for (s_, k) in want:
        (d["threads"][s_] if s_ else d["threads"]).pop(k, None)
    return d
assert strip(o) == strip(n), "la edicion cambia algo mas que threads"
open(path, "w", encoding="utf-8").write(new)
print("config.template.yml threads: " + ", ".join(f"{'.'.join(p for p in k if p)} {found[k]}->{want[k]}"
                                                  for k in want if found[k] != str(want[k])))
EOF

tar --owner=0 --group=0 -czf "$OUT/$BUNDLE" -C "$WORK/x" teaspeak_v2_bundle
cp -f "$WORK/b1/$GEO" "$OUT/$GEO"
(cd "$OUT" && sha256sum "$BUNDLE" "$GEO" > SHA256SUMS)

# verificacion: el bundle preparado trae Build 2
rm -rf "$WORK/verify"
mkdir "$WORK/verify"
tar -xzf "$OUT/$BUNDLE" -C "$WORK/verify" teaspeak_v2_bundle/TeaSpeakServer teaspeak_v2_bundle/config.template.yml
got=$(md5sum < "$WORK/verify/teaspeak_v2_bundle/TeaSpeakServer" | cut -d' ' -f1)
[ "$got" = "$B2_MD5" ] || { echo "VERIFICACION FALLIDA: md5 $got"; exit 1; }
echo "OK: $OUT"
ls -la "$OUT"
cat "$OUT/SHA256SUMS"
