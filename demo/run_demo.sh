#!/usr/bin/env bash
# Starts the demo on the installed MySQL server (:3306): profiler (:3317 -> :3306, dashboard :8780)
# (ports: MYSQL_PORT, PROXY_PORT, UI_PORT, APP_PORT)
# and the Mini Shop app (:8090). Ctrl+C stops the profiler and the app; MySQL is not touched.
#
# Once, as the MySQL administrator:   sudo mysql < demo/mysql_user.sql
#
#   demo/run_demo.sh            # keep the existing demo data
#   demo/run_demo.sh --fresh    # recreate the demo database
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
UI_PORT=${UI_PORT:-8780}
APP_PORT=${APP_PORT:-8090}
MYSQL_PORT=${MYSQL_PORT:-3306}
PROXY_PORT=${PROXY_PORT:-3317}

if ! $PY -c "import mysql.connector, aiohttp, sqlglot" 2>/dev/null; then
  echo "missing Python packages: run   $PY -m pip install -r requirements.txt -r demo/requirements.txt"
  exit 1
fi
for port in "$PROXY_PORT" "$UI_PORT" "$APP_PORT"; do
  if $PY -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1', $port)) != 0)"; then
    echo "port $port is already in use (another profiler or app still running?)."
    echo "Stop it, or choose other ports:  PROXY_PORT=3318 UI_PORT=8781 APP_PORT=8091 demo/run_demo.sh"
    exit 1
  fi
done

# Direct login to MySQL: checks the demo user and, with caching_sha2_password (MySQL 8), puts it in the
# server's authentication cache, so the clear-text logins through the profiler can use the fast path.
tables=$($PY - "$MYSQL_PORT" <<'PY'
import sys, mysql.connector
try:
    c = mysql.connector.connect(host="127.0.0.1", port=int(sys.argv[1]), user="shop", password="Shop-Demo-2026")
except mysql.connector.Error as e:
    sys.exit(f"cannot log in to MySQL on 127.0.0.1:{sys.argv[1]} as shop / Shop-Demo-2026: {e}\n"
             "is MySQL running? create the demo user once with:   sudo mysql < demo/mysql_user.sql")
cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'shop_demo'")
print(cur.fetchone()[0])
PY
)
if [[ "${1:-}" == "--fresh" ]] || [[ "$tables" == "0" ]]; then
  $PY demo/setup_db.py --port "$MYSQL_PORT"
fi

$PY -m sqlprofiler run --proxy mysql:$PROXY_PORT:127.0.0.1:$MYSQL_PORT --schema demo/schema.sql \
    --profiles demo/profiles --ui-port "$UI_PORT" &
PROFILER=$!
# start the app only once the proxy accepts connections
for _ in $(seq 1 40); do
  $PY -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1', $PROXY_PORT)) != 0)" && break
  sleep 0.25
done
$PY demo/shop_app.py --port "$APP_PORT" --db-port "$PROXY_PORT" &
APP=$!
trap 'kill $APP $PROFILER 2>/dev/null; wait 2>/dev/null; echo; echo "stopped"' EXIT INT TERM

sleep 2
cat <<MSG

  Profiler dashboard : http://127.0.0.1:$UI_PORT
  Mini Shop app      : http://127.0.0.1:$APP_PORT   (login admin/admin or enrico/enrico)

  Record actions by hand in the dashboard, or play the automatic scenario in another terminal:
      $PY demo/scenario.py --profiler http://127.0.0.1:$UI_PORT --app http://127.0.0.1:$APP_PORT

  Ctrl+C to stop.
MSG
wait
