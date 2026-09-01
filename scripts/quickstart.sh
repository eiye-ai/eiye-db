#!/usr/bin/env bash
# Frictionless quickstart: set up the environment (once) and run the walkthrough.
#
#   scripts/quickstart.sh --name notes --type filesystem  --root ~/Documents/exports
#   scripts/quickstart.sh --name db    --type postgresql  --dsn postgresql://user:pass@host/db
#   scripts/quickstart.sh --name app   --type sqlite      --path ~/app.db
#   scripts/quickstart.sh --name files --type s3          --bucket my-exports
#   scripts/quickstart.sh --name api   --type rest_api    --url https://api.example.com
#
# --help lists every type; connectors with an optional driver (mysql, sqlserver,
# s3) also need their extra: pip install -e "backend[mysql]".
#
# Creates backend/.venv and editable-installs eiye_db on first run, then hands off
# to scripts/quickstart.py (register -> discover -> governed query -> connect an agent).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$REPO/backend"
PY="$BACKEND/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "→ creating venv at $BACKEND/.venv"
  python3 -m venv "$BACKEND/.venv"
fi

# Editable-install so `eiye_db` (and `python -m eiye_db.mcp_server`) resolve from any cwd.
if ! "$PY" -c "import eiye_db" 2>/dev/null; then
  echo "→ installing eiye_db (editable)…"
  "$BACKEND/.venv/bin/pip" install -e "$BACKEND" -q
fi

exec "$PY" "$REPO/scripts/quickstart.py" "$@"
