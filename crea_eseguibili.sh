#!/usr/bin/env bash
# Crea gli eseguibili di SQL Profiler e li copia in una cartella sul Desktop.
#
#   ./crea_eseguibili.sh              Linux e Windows
#   ./crea_eseguibili.sh linux        solo Linux   (.AppImage e .tar.gz)
#   ./crea_eseguibili.sh windows      solo Windows (.exe portatile)
#
# Serve Node.js (https://nodejs.org) e internet la prima volta. Su Windows usare crea_eseguibili.bat.
# Il computer su cui si usa l'app ha comunque bisogno di Python 3.10 o più recente.
set -euo pipefail
shopt -s nullglob

CARTELLA="$(cd "$(dirname "$0")" && pwd)"
errore() { printf '\n[ERRORE] %s\n' "$1" >&2; exit 1; }

case "${1:-tutti}" in
  linux)                    COMANDO=dist;     TIPI=(AppImage tar.gz) ;;
  windows|win)              COMANDO=dist:win; TIPI=(exe) ;;
  tutti|all)                COMANDO=dist:all; TIPI=(AppImage tar.gz exe) ;;
  *) errore "Scelta sconosciuta: $1. Usare: linux, windows oppure nessuna (entrambi)." ;;
esac
[ "$(uname -s)" = "Linux" ] || errore "Questo script è per Linux. Su Windows usare crea_eseguibili.bat."
command -v npm >/dev/null || errore "Node.js non è installato: scaricarlo da https://nodejs.org e riprovare."

cd "$CARTELLA/electron"
if [ ! -d node_modules ]; then
  echo "== Preparazione (la prima volta scarica gli strumenti, qualche minuto)"
  npm ci --no-audit --no-fund || errore "Strumenti non scaricati: controllare la connessione a internet."
fi

echo "== Creazione degli eseguibili (la versione Windows può richiedere circa 10 minuti)"
npm run "$COMANDO" || errore "Creazione non riuscita: guardare i messaggi qui sopra."

cd dist
PRONTI=()
for tipo in "${TIPI[@]}"; do PRONTI+=(SQL-Profiler-*."$tipo"); done
[ "${#PRONTI[@]}" -gt 0 ] || errore "Non ho trovato gli eseguibili in electron/dist."

DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
[ -d "$DESKTOP" ] || DESKTOP="$CARTELLA"
DESTINAZIONE="$DESKTOP/SQL Profiler - eseguibili"
mkdir -p "$DESTINAZIONE"
cp -f "${PRONTI[@]}" "$DESTINAZIONE/"

echo
echo "============================================================"
echo "  OK - ESEGUIBILI PRONTI"
echo "============================================================"
echo "Cartella: $DESTINAZIONE"
for f in "${PRONTI[@]}"; do printf '  %s (%s)\n' "$f" "$(du -h "$f" | cut -f1)"; done
