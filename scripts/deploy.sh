#!/usr/bin/env bash
#
# Deploy custom_components/perific to the real Home Assistant instance.
#
# Packages the component, ships it, swaps it in atomically, restarts Home Assistant
# through its REST API, and fails loudly with the relevant log lines if the entities
# don't come back. On failure it puts the previous version back.
#
# Configuration comes from .env in the repository root — see README.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPONENT="$REPO_ROOT/custom_components/perific"
ENV_FILE="$REPO_ROOT/.env"

RESTART_TIMEOUT=${RESTART_TIMEOUT:-180}
POLL_SECONDS=5

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# --- configuration ----------------------------------------------------------

[ -f "$ENV_FILE" ] || fail "No .env at $ENV_FILE"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${HA_URL:?HA_URL is not set in .env (e.g. http://homeassistant.local)}"
: "${HA_TOKEN:?HA_TOKEN is not set in .env (Profile -> Long-lived access tokens)}"
: "${HA_SSH:?HA_SSH is not set in .env (e.g. martin@homeassistant.local)}"
: "${HA_CONFIG_DIR:?HA_CONFIG_DIR is not set in .env (the host path bind-mounted to /config)}"

# Matched against entity_id to decide whether the deploy worked.
HA_VERIFY_ENTITY=${HA_VERIFY_ENTITY:-energy_import}

HA_URL=${HA_URL%/}
REMOTE_COMPONENTS="$HA_CONFIG_DIR/custom_components"
REMOTE_TARBALL="/tmp/perific-deploy-$$.tar.gz"

api() {
  local method=$1 path=$2
  shift 2
  curl -fsS -X "$method" \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    "$HA_URL$path" "$@"
}

# --- preflight --------------------------------------------------------------

step "Checking the component"
[ -f "$COMPONENT/manifest.json" ] || fail "No manifest.json in $COMPONENT"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$COMPONENT/manifest.json" \
  || fail "manifest.json is not valid JSON"
VERSION=$(python3 -c "import json;print(json.load(open('$COMPONENT/manifest.json'))['version'])")
echo "perific $VERSION"

step "Checking Home Assistant is reachable"
api GET /api/ >/dev/null || fail "Cannot reach $HA_URL — check HA_URL and HA_TOKEN"

# --- package and ship -------------------------------------------------------

STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
TARBALL="$STAGING/perific.tar.gz"

step "Packaging"
# --exclude keeps local bytecode and caches out of what ships.
tar -czf "$TARBALL" \
  --exclude='__pycache__' --exclude='*.pyc' \
  -C "$REPO_ROOT/custom_components" perific

step "Uploading"
scp -q "$TARBALL" "$HA_SSH:$REMOTE_TARBALL"

step "Swapping it in"
# Unpacked beside the live directory and moved into place, so a failed transfer
# never leaves a half-written component for Home Assistant to import.
ssh "$HA_SSH" bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_COMPONENTS"
rm -rf .perific-new .perific-old
mkdir .perific-new
tar -xzf "$REMOTE_TARBALL" -C .perific-new --strip-components=1
test -f .perific-new/manifest.json
if [ -d perific ]; then mv perific .perific-old; fi
mv .perific-new perific
rm -f "$REMOTE_TARBALL"
REMOTE

rollback() {
  printf '\033[31mRolling back\033[0m\n' >&2
  ssh "$HA_SSH" bash -s <<REMOTE || true
set -euo pipefail
cd "$REMOTE_COMPONENTS"
if [ -d .perific-old ]; then rm -rf perific; mv .perific-old perific; fi
REMOTE
  api POST /api/services/homeassistant/restart -d '{}' >/dev/null || true
}

# --- restart and verify -----------------------------------------------------

step "Restarting Home Assistant"
api POST /api/services/homeassistant/restart -d '{}' >/dev/null

step "Waiting for the entities to come back"
deadline=$((SECONDS + RESTART_TIMEOUT))
found=""
while [ $SECONDS -lt $deadline ]; do
  sleep "$POLL_SECONDS"
  states=$(api GET /api/states 2>/dev/null) || continue
  found=$(printf '%s' "$states" | python3 -c "
import json, sys
pattern = sys.argv[1]
try:
    states = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print('\n'.join(s['entity_id'] for s in states if pattern in s['entity_id']))
" "$HA_VERIFY_ENTITY")
  [ -n "$found" ] && break
done

if [ -z "$found" ]; then
  printf '\033[31mNo entity matching "%s" appeared within %ss\033[0m\n' \
    "$HA_VERIFY_ENTITY" "$RESTART_TIMEOUT" >&2
  echo "--- last log lines mentioning perific ---" >&2
  api GET /api/error_log 2>/dev/null | grep -i perific | tail -30 >&2 || true
  rollback
  exit 1
fi

step "Deployed"
echo "$found"
echo
echo "Now check by hand:"
echo "  - Developer Tools -> States: unit and state_class on the entity above"
echo "  - Entity settings: a statistics graph (absence means the typing was rejected)"
echo "  - Developer Tools -> Statistics: no issues listed"
echo "  - Settings -> Energy: the sensor is selectable as grid consumption"
echo "  - 48 hours later: hourly statistics still accumulating"
