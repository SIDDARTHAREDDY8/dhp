#!/usr/bin/env bash
# DHP one-line installer: pip install + MCP registration + supervisor setup.
#
#   curl -fsSL https://raw.githubusercontent.com/SIDDARTHAREDDY8/dhp/main/install.sh | bash
#
# Flags:
#   --root DIR        DHP data directory (default: ~/.dhp)
#   --no-mcp          skip MCP client registration
#   --supervisor      also start a supervisor in the background (nohup)

set -euo pipefail

ROOT="${DHP_ROOT:-$HOME/.dhp}"
NO_MCP=0
WITH_SUPERVISOR=0

for arg in "$@"; do
  case "$arg" in
    --root=*) ROOT="${arg#--root=}" ;;
    --root) shift; ROOT="$1" ;;
    --no-mcp) NO_MCP=1 ;;
    --supervisor) WITH_SUPERVISOR=1 ;;
  esac
done

echo "=== DHP installer ==="

# 1. Python check
if ! command -v python3 >/dev/null; then
  echo "error: python3 not found" >&2; exit 1
fi

# 2. Install the package with MCP extras
echo "-> pip install dhp-protocol[mcp]"
PIP_FLAGS="--break-system-packages --ignore-installed"
if command -v pipx >/dev/null; then
  pipx install "dhp-protocol[mcp]" 2>/dev/null \
    || pipx reinstall "dhp-protocol[mcp]" 2>/dev/null \
    || python3 -m pip install -q $PIP_FLAGS "dhp-protocol[mcp]"
else
  python3 -m pip install -q $PIP_FLAGS "dhp-protocol[mcp]" 2>/dev/null \
    || python3 -m pip install -q "dhp-protocol[mcp]"
fi

# 3. Data directory
mkdir -p "$ROOT"
echo "-> data dir: $ROOT"

# 4. Register with Claude Code if available
if [ "$NO_MCP" -eq 0 ] && command -v claude >/dev/null; then
  if claude mcp list 2>/dev/null | grep -q "^dhp"; then
    echo "-> Claude Code: 'dhp' MCP server already registered"
  else
    claude mcp add dhp -- dhp-mcp --root "$ROOT" 2>/dev/null \
      && echo "-> Claude Code: registered 'dhp' MCP server" \
      || echo "-> Claude Code: registration failed (run manually: claude mcp add dhp -- dhp-mcp --root $ROOT)"
  fi
elif [ "$NO_MCP" -eq 0 ]; then
  echo "-> Claude Code CLI not found; add manually:"
  echo "   claude mcp add dhp -- dhp-mcp --root $ROOT"
fi

# 5. Optional supervisor
if [ "$WITH_SUPERVISOR" -eq 1 ]; then
  if pgrep -f "dhp-supervisor $ROOT" >/dev/null; then
    echo "-> supervisor already running on $ROOT"
  else
    nohup dhp-supervisor "$ROOT" sup1 86400 >/dev/null 2>&1 &
    echo "-> supervisor started (leases watched, dead workers orphaned in ~7s)"
  fi
fi

echo ""
echo "Done. Try it:"
echo "  python3 -c \"import dhp; s=dhp.Store('$ROOT'); print(dhp.dispatch(s, task_kind='demo', inputs={}, output_schema={'type':'object'}))\""
echo ""
echo "Docs: https://github.com/SIDDARTHAREDDY8/dhp"
