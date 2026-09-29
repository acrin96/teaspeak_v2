#!/bin/bash
# Prepara (SIN publicar) los assets de la release v1.4.21-beta-3-build2.1 de acrin96/teaspeak_v2:
#   - mismo bundle que v1.4.21-beta-3-build2 (mismos nombres de asset, que es lo que baja install.sh desde
#     releases/latest) con SOLO el binario sustituido por Build 2.1 (md5 11e755ad); config.template.yml y el
#     resto del bundle sin cambios (los hilos tuned ya venian en build2);
#   - geoloc sin cambios; SHA256SUMS regenerado.
# Deja los ficheros en /root/window-build21/release/. Los publica post_build21.py (solo si la ventana sale OK).
# Solo lee de GitHub y escribe en /root/work/rel_build21 y /root/window-build21/release; no toca produccion.
set -euo pipefail
B2_TAG=v1.4.21-beta-3-build2
OUT=/root/window-build21/release
WORK=/root/work/rel_build21
B2_MD5=ae2a571c74fa997f84d215104af9f169
B21=/root/build-out/build21/TeaSpeakServer.build21
B21_MD5=11e755ad0d84e644e151b056add0a58d
BUNDLE=teaspeak_v2_1.4.21-beta-3_linux_amd64.tar.gz
GEO=teaspeak_v2_geoloc.tar.gz

mkdir -p "$WORK/b2" "$OUT"
for a in "$BUNDLE" "$GEO" SHA256SUMS; do
    curl -fsSL -o "$WORK/b2/$a" "https://github.com/acrin96/teaspeak_v2/releases/download/$B2_TAG/$a"
done
(cd "$WORK/b2" && sha256sum -c SHA256SUMS)

[ "$(md5sum < "$B21" | cut -d' ' -f1)" = "$B21_MD5" ] || { echo "binario Build 2.1 con md5 inesperado"; exit 1; }
rm -rf "$WORK/x"
mkdir "$WORK/x"
tar -xzf "$WORK/b2/$BUNDLE" -C "$WORK/x"
T="$WORK/x/teaspeak_v2_bundle"
[ "$(md5sum < "$T/TeaSpeakServer" | cut -d' ' -f1)" = "$B2_MD5" ] || { echo "el bundle de build2 no trae Build 2"; exit 1; }
install -m 755 "$B21" "$T/TeaSpeakServer"

tar --owner=0 --group=0 -czf "$OUT/$BUNDLE" -C "$WORK/x" teaspeak_v2_bundle
cp -f "$WORK/b2/$GEO" "$OUT/$GEO"
(cd "$OUT" && sha256sum "$BUNDLE" "$GEO" > SHA256SUMS)

# verificacion: el bundle preparado trae Build 2.1 y solo cambia el binario respecto a build2
rm -rf "$WORK/verify" "$WORK/verify_b2"
mkdir "$WORK/verify" "$WORK/verify_b2"
tar -xzf "$OUT/$BUNDLE" -C "$WORK/verify"
tar -xzf "$WORK/b2/$BUNDLE" -C "$WORK/verify_b2"
got=$(md5sum < "$WORK/verify/teaspeak_v2_bundle/TeaSpeakServer" | cut -d' ' -f1)
[ "$got" = "$B21_MD5" ] || { echo "VERIFICACION FALLIDA: md5 $got"; exit 1; }
diffs=$(diff -rq "$WORK/verify_b2/teaspeak_v2_bundle" "$WORK/verify/teaspeak_v2_bundle" || true)
echo "diferencias con build2: $diffs"
[ "$(echo "$diffs" | grep -c .)" = 1 ] && echo "$diffs" | grep -q 'TeaSpeakServer differ' || { echo "VERIFICACION FALLIDA: cambia algo mas que el binario"; exit 1; }
rm -rf "$WORK/verify" "$WORK/verify_b2" "$WORK/x"
echo "OK: $OUT"
ls -la "$OUT"
cat "$OUT/SHA256SUMS"
