#!/usr/bin/env bash
# Run the Windows venv Python from WSL. WSL paths in arguments that exist are
# converted to Windows (UNC) paths (relative ones too). Usage: tools/winpy.sh script.py args...
#        tools/winpy.sh -m pip install foo
set -euo pipefail
PY="$(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')")/airpod-venv/Scripts/python.exe"
args=()
for a in "$@"; do
  if [[ "$a" != -* && -e "$a" ]]; then args+=("$(wslpath -w "$(realpath "$a")")"); else args+=("$a"); fi
done
cd /mnt/c  # avoid "UNC paths are not supported" cwd warnings
PYTHONUNBUFFERED=1 WSLENV="${WSLENV:+$WSLENV:}PYTHONUNBUFFERED:BUMBLE_LOGLEVEL:AIRPODLINK_SC" exec "$PY" "${args[@]}"
