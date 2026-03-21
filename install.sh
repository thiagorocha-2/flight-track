#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 nao encontrado. Instale Python 3.10+."
  exit 1
fi

python3 -m venv .venv
# shellcheck disable=SC1091
source ".venv/bin/activate"

python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium

PLIST_SRC="${ROOT}/com.flighttrack.daily.plist"
PLIST_NAME="com.flighttrack.daily.plist"
DEST_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${DEST_DIR}/${PLIST_NAME}"
LOG_DIR="${HOME}/Library/Logs"

mkdir -p "${DEST_DIR}" "${LOG_DIR}"

sed \
  -e "s|FLIGHT_TRACK_HOME|${ROOT}|g" \
  -e "s|FLIGHT_TRACK_LOG_DIR|${LOG_DIR}|g" \
  "${PLIST_SRC}" > "${PLIST_DEST}"

launchctl unload "${PLIST_DEST}" 2>/dev/null || true
launchctl load "${PLIST_DEST}"

echo ""
echo "Flight Track instalado."
echo "  LaunchAgent: ${PLIST_DEST}"
echo "  Execucao diaria: 09:00 (horario local do Mac)"
echo "  Logs app: ${LOG_DIR}/flight-track.log"
echo "  Logs launchd: ${LOG_DIR}/flight-track-launchd.out.log / .err.log"
echo ""
echo "Configure .env e flights.json antes da primeira execucao."
