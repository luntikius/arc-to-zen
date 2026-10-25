#!/bin/sh
# Local synthetic preview. No download, browser discovery or browser writes.
set -eu
ROOT=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd)
PYTHON="$HOME/Library/Application Support/ArcToZenTool/current/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PYTHON="$ROOT/.venv/bin/python";
  else printf '%s\n' 'Create the local dev environment with uv sync first.' >&2; exit 1; fi
fi
export PYTHONPATH="$ROOT"
export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON" -m arc_to_zen.cli wizard --demo --dry-run "$@"
