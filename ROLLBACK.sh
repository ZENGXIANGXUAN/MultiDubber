#!/usr/bin/env bash
set -euo pipefail

TARGET_PATH="${1:-runTTS.bat}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIBLING_BASELINE="${SCRIPT_DIR}/runTTS.bat.baseline"

if [ -f "${SIBLING_BASELINE}" ]; then
    cp -f "${SIBLING_BASELINE}" "${TARGET_PATH}"
else
    cat <<'EOF' > "${TARGET_PATH}"
@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo ERROR: .venv\Scripts\pythonw.exe was not found.
    echo Please create or repair the project virtual environment first.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0gui.pyw"
exit /b 0
EOF
fi

echo "[ROLLBACK.sh] Successfully restored pristine baseline bytes to ${TARGET_PATH}"
