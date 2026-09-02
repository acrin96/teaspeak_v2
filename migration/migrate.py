#!/usr/bin/env python3
# Migra los datos de un TeaData.sqlite (v15) a la base PostgreSQL de TeaSpeak v2.
# Uso:  sudo -u postgres python3 migrate.py <ruta TeaData.sqlite> [dbname]
#
# Reemplaza por completo los datos de la instalacion por defecto por los de produccion.
# Mismo esquema v15: los nombres de columna de SQLite (camelCase) mapean a PostgreSQL
# en minuscula; todas las columnas se citan con comillas dobles (palabras reservadas
# como grant/key/value/type/until/read).
import sqlite3, sys
import psycopg2
from psycopg2.extras import execute_values

sqlite_path = sys.argv[1]
dbname = sys.argv[2] if len(sys.argv) > 2 else "teaspeak"

# Tablas a migrar (nombre en SQLite; en PostgreSQL es el mismo en minuscula).
# properties_v6 / permissions_v6 se omiten (vacias y no existen en PostgreSQL).
TABLES = ["servers","channels","groups","clients","clients_server","assignedGroups",
          "permissions","properties","bannedClients","ban_trigger","tokens","queries",
          "complains","letters","musicbots","playlists","playlist_songs",
          "conversations","conversation_blocks","general"]

# columnas IDENTITY en PostgreSQL cuya secuencia hay que reajustar tras insertar
IDENTITY = {"clients":"client_database_id", "general":"id", "groups":"groupid"}

sq = sqlite3.connect(sqlite_path)
pg = psycopg2.connect("dbname=%s" % dbname)   # socket local, peer como usuario postgres
pg.autocommit = False
cur = pg.cursor()

# tablas realmente presentes en el sqlite
present = {r[0] for r in sq.execute("SELECT name FROM sqlite_master WHERE type='table'")}

total = 0
for t in TABLES:
    if t not in present:
        print("  omito %s (no esta en el sqlite)" % t); continue
    cols = [r[1] for r in sq.execute('PRAGMA table_info("%s")' % t).fetchall()]
    if not cols:
        print("  omito %s (sin columnas)" % t); continue
    lt = t.lower()
    sel = ",".join('"%s"' % c for c in cols)
    ins_cols = ",".join('"%s"' % c.lower() for c in cols)
    rows = sq.execute('SELECT %s FROM "%s"' % (sel, t)).fetchall()
    cur.execute('TRUNCATE "%s"' % lt)
    if rows:
        tmpl = "(" + ",".join(["%s"] * len(cols)) + ")"
        execute_values(cur, 'INSERT INTO "%s" (%s) VALUES %%s' % (lt, ins_cols),
                       rows, template=tmpl, page_size=1000)
    print("  %-20s %8d filas" % (lt, len(rows)))
    total += len(rows)

# reajustar secuencias de las columnas IDENTITY para no colisionar con futuras inserciones
for lt, col in IDENTITY.items():
    cur.execute("SELECT setval(pg_get_serial_sequence('%s','%s'), "
                "COALESCE((SELECT MAX(\"%s\") FROM \"%s\"),1))" % (lt, col, col, lt))

pg.commit()
print("TOTAL %d filas migradas. COMMIT OK." % total)
sq.close(); pg.close()
