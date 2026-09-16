#!/usr/bin/env bash
#
# Deploy custom_components/perific to the real Home Assistant instance.
#
# Packages the component, ships it, swaps it in atomically, restarts Home Assistant
# through its REST API, and fails loudly with the relevant log lines if the
# integration doesn't load again. On failure it puts the previous version back.
#
# Configuration comes from .env in the repository root — see README.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPONENT="$REPO_ROOT/custom_components/perific"
ENV_FILE="$REPO_ROOT/.env"

DOMAIN=perific
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

# Optional extra check: a substring an entity_id must contain once the integration is
# up. Empty by default, because entity IDs are built from translated names and so
# differ with the instance's language.
HA_VERIFY_ENTITY=${HA_VERIFY_ENTITY:-}

HA_URL=${HA_URL%/}
REMOTE_COMPONENTS="$HA_CONFIG_DIR/custom_components"
# The previous version is kept out of the config directory, which on a container
# install is often not writable by the SSH user even when custom_components is. The
# default is deliberately left for the remote shell to expand.
REMOTE_BACKUP=${HA_DEPLOY_DIR:-\$HOME/.perific-deploy}
REMOTE_TARBALL="/tmp/perific-deploy-$$.tar.gz"
# Computed here so the swap and the rollback name the same backup.
STAMP=$(date +%Y%m%d-%H%M%S)

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
# --exclude keeps local bytecode and caches out of what ships. --no-xattrs keeps
# macOS provenance attributes out of the pax headers, which GNU tar on the far end
# warns about once per file.
tar -czf "$TARBALL" \
  --no-xattrs --exclude='__pycache__' --exclude='*.pyc' \
  -C "$REPO_ROOT/custom_components" perific

step "Uploading"
scp -q "$TARBALL" "$HA_SSH:$REMOTE_TARBALL"

step "Swapping it in"
# Unpacked beside the live directory and moved into place, so a failed transfer never
# leaves a half-written component for Home Assistant to import.
ssh "$HA_SSH" bash -s <<REMOTE
set -euo pipefail
BACKUP="$REMOTE_BACKUP"
COMPONENTS="$REMOTE_COMPONENTS"
HOLDING="\$COMPONENTS/perific_deploy_tmp"

mkdir -p "\$BACKUP" "\$COMPONENTS"
rm -rf "\$HOLDING"
mkdir "\$HOLDING"

# Unpacked one level below where the scan looks. Home Assistant reads
# <dir>/manifest.json for every directory in custom_components and skips those
# without one, so the holding directory stays invisible while the files land.
tar -xzf "$REMOTE_TARBALL" -C "\$HOLDING"
test -f "\$HOLDING/perific/manifest.json"

# Names beginning with a dot are scanned too, and resolve to the empty module
# "custom_components.", which breaks every custom integration on the instance.
# Moved rather than deleted, for the same reason as the backup below.
for stale in "\$COMPONENTS"/.perific-*; do
  [ -e "\$stale" ] || continue
  mv "\$stale" "\$BACKUP/stale-$STAMP-\$(basename "\$stale")"
done

# Never delete a directory Home Assistant has imported from. It writes __pycache__ as
# root, and unlinking a file needs write permission on its parent directory, so this
# user cannot remove those. Renaming only needs it on the two directories involved.
if [ -d "\$COMPONENTS/perific" ]; then
  mv "\$COMPONENTS/perific" "\$BACKUP/perific-$STAMP"
  echo "previous version kept at \$BACKUP/perific-$STAMP"
fi
# A rename within one directory, so the live component is never half-written.
mv "\$HOLDING/perific" "\$COMPONENTS/perific"
# Past this point the deploy has happened, so cleanup must not be able to fail it.
# The holding directory holds nothing Home Assistant ever imported.
rm -rf "\$HOLDING" || true
rm -f "$REMOTE_TARBALL" || true

# Best effort: keep the five most recent backups, ignoring any that resist deletion.
# The trailing guard matters: pipefail would turn an unmatched glob into a failure
# that aborts a deploy which has already succeeded.
ls -1dt "\$BACKUP"/perific-* 2>/dev/null | tail -n +6 | while read -r old; do
  rm -rf "\$old" 2>/dev/null || true
done || true

stray=\$(find "\$COMPONENTS" -mindepth 1 -maxdepth 1 -type d -name '.*')
if [ -n "\$stray" ]; then
  echo "Directories in custom_components that Home Assistant cannot import:" >&2
  echo "\$stray" >&2
  exit 1
fi
REMOTE

rollback() {
  printf '\033[31mRolling back\033[0m\n' >&2
  ssh "$HA_SSH" bash -s <<REMOTE || true
set -euo pipefail
BACKUP="$REMOTE_BACKUP"
COMPONENTS="$REMOTE_COMPONENTS"
if [ -d "\$BACKUP/perific-$STAMP" ]; then
  # Moved aside rather than deleted: the version being replaced may already carry
  # root-owned bytecode that this user cannot unlink.
  if [ -d "\$COMPONENTS/perific" ]; then
    mv "\$COMPONENTS/perific" "\$BACKUP/failed-$STAMP"
  fi
  mv "\$BACKUP/perific-$STAMP" "\$COMPONENTS/perific"
fi
REMOTE
  api POST /api/services/homeassistant/restart -d '{}' >/dev/null || true
}

# --- restart and verify -----------------------------------------------------

step "Restarting Home Assistant"
api POST /api/services/homeassistant/restart -d '{}' >/dev/null

step "Waiting for the integration to load"
# The config entry's state is the honest check: it is what Home Assistant reports
# after actually importing and setting the component up, and unlike an entity ID it
# does not change with the instance's language.
deadline=$((SECONDS + RESTART_TIMEOUT))
loaded=""
while [ $SECONDS -lt $deadline ]; do
  sleep "$POLL_SECONDS"
  entries=$(api GET "/api/config/config_entries/entry?domain=$DOMAIN" 2>/dev/null) || continue
  loaded=$(printf '%s' "$entries" | python3 -c "
import json, sys
try:
    entries = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print('\n'.join(e['title'] for e in entries if e.get('state') == 'loaded'))
") || loaded=""
  [ -n "$loaded" ] && break
done

if [ -z "$loaded" ]; then
  printf '\033[31mNo loaded %s config entry within %ss\033[0m\n' "$DOMAIN" "$RESTART_TIMEOUT" >&2
  echo "--- last log lines mentioning perific ---" >&2
  api GET /api/error_log 2>/dev/null | grep -i perific | tail -30 >&2 || true
  rollback
  exit 1
fi
echo "config entry loaded: $loaded"

if [ -n "$HA_VERIFY_ENTITY" ]; then
  step "Looking for an entity matching \"$HA_VERIFY_ENTITY\""
  found=$(api GET /api/states | python3 -c "
import json, sys
pattern = sys.argv[1]
print('\n'.join(s['entity_id'] for s in json.load(sys.stdin) if pattern in s['entity_id']))
" "$HA_VERIFY_ENTITY")
  [ -n "$found" ] || { printf '\033[31mNothing matched\033[0m\n' >&2; rollback; exit 1; }
  echo "$found"
fi

step "Deployed"
echo
echo "Now check by hand:"
echo "  - Developer Tools -> States: unit and state_class on the entities"
echo "  - Entity settings: a statistics graph (absence means the typing was rejected)"
echo "  - Developer Tools -> Statistics: no issues listed"
echo "  - Settings -> Energy: import and export selectable, power under the two-sensor mode"
echo "  - 48 hours later: hourly statistics still accumulating"
