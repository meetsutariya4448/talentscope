#!/usr/bin/env bash
# Roll the deployed stack back to a previous image tag.
#
#   bash scripts/rollback.sh              # back to PREVIOUS_TAG in .deploy-state
#   bash scripts/rollback.sh a1b2c3d      # back to a specific tag
#   bash scripts/rollback.sh --list       # tags available locally
#
# This works because scripts/deploy.sh deploys immutable git-SHA tags and
# records what it replaced. It could not work against `talentscope:latest`,
# which is what everything used before: there is no way to ask for "the
# previous latest".
#
# Deliberately does NOT roll back the database. An Alembic migration that has
# already run is not undone by pointing at an older image, and quietly running
# `alembic downgrade` during an incident is how a bad deploy becomes data loss.
# If the bad deploy migrated, that is a restore (scripts/restore_db.sh), not a
# rollback, and the script says so rather than guessing.
set -euo pipefail
cd "$(dirname "$0")/.."

STATE_FILE=".deploy-state"
COMPOSE_FILE="docker-compose.deploy.yml"

if [ "${1:-}" = "--list" ]; then
  echo "Local talentscope image tags:"
  docker images talentscope --format '  {{.Tag}}\t{{.CreatedSince}}\t{{.Size}}' | grep -v '^  <none>'
  [ -f "$STATE_FILE" ] && { echo ""; echo "State:"; grep -E '^(CURRENT_TAG|PREVIOUS_TAG|DEPLOYED_AT)=' "$STATE_FILE" | sed 's/^/  /'; }
  exit 0
fi

[ -f "$STATE_FILE" ] || { echo "No ${STATE_FILE}; nothing has been deployed by scripts/deploy.sh." >&2; exit 1; }

RECORDED_TAG="$(grep '^CURRENT_TAG=' "$STATE_FILE" | cut -d= -f2- || true)"
PREVIOUS_TAG="$(grep '^PREVIOUS_TAG=' "$STATE_FILE" 2>/dev/null | cut -d= -f2- || true)"

# What is *actually* running, read from the container rather than trusted from
# the state file. scripts/deploy.sh only writes state after the deployment
# verifies healthy, so a failed deploy leaves the file describing the last
# good version while a broken image is the thing running. Rolling back to
# PREVIOUS_TAG in that situation skips over the last known-good version and
# lands one release too far back — found during the rollback drill on
# 2026-09-07, where a failed deploy of talentscope:broken rolled back past the
# healthy tag it should have returned to.
RUNNING_TAG="$(docker inspect --format '{{.Config.Image}}' talentscope-deploy-api-1 2>/dev/null | sed 's/^talentscope://' || true)"

CURRENT_TAG="${RUNNING_TAG:-$RECORDED_TAG}"

# ${1:-} rather than $1: `set -u` makes a bare $1 an unbound-variable error
# when the script is invoked with no argument, which is its most common form.
if [ -n "${1:-}" ]; then
  TARGET_TAG="$1"
elif [ -n "$RUNNING_TAG" ] && [ "$RUNNING_TAG" != "$RECORDED_TAG" ]; then
  # Running something the state file never recorded as good: the last
  # successful deployment is the right target, not the one before it.
  TARGET_TAG="$RECORDED_TAG"
  echo "  running talentscope:${RUNNING_TAG}, last verified-good is ${RECORDED_TAG}"
  echo "  (a failed deploy does not update ${STATE_FILE}) -> rolling back to ${RECORDED_TAG}"
else
  TARGET_TAG="$PREVIOUS_TAG"
fi

if [ -z "$TARGET_TAG" ]; then
  echo "No previous tag recorded in ${STATE_FILE} and none given." >&2
  echo "Available tags: bash scripts/rollback.sh --list" >&2
  exit 1
fi

if [ "$TARGET_TAG" = "$CURRENT_TAG" ]; then
  echo "Already running talentscope:${TARGET_TAG} — nothing to do."
  exit 0
fi

if ! docker image inspect "talentscope:${TARGET_TAG}" >/dev/null 2>&1; then
  echo "ERROR: talentscope:${TARGET_TAG} is not present locally." >&2
  echo "A rollback target must already exist as an image — this is not the moment to build one." >&2
  exit 1
fi

echo "=== Rolling back: ${CURRENT_TAG:-unknown} -> ${TARGET_TAG} ==="

# Reuse the credentials the current deployment is running with. Regenerating
# the Postgres password here would leave the app unable to reach its own
# database, turning a rollback into a second outage.
POSTGRES_PASSWORD="$(grep '^POSTGRES_PASSWORD=' "$STATE_FILE" | cut -d= -f2-)"
GRAFANA_ADMIN_PASSWORD="$(grep '^GRAFANA_ADMIN_PASSWORD=' "$STATE_FILE" | cut -d= -f2-)"
export POSTGRES_PASSWORD GRAFANA_ADMIN_PASSWORD
export GROQ_API_KEY="${GROQ_API_KEY:-$(grep -E '^GROQ_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)}"

CURRENT_HEAD="$(docker compose -f "$COMPOSE_FILE" exec -T api alembic current 2>/dev/null | tail -1 || echo 'unknown')"

export TALENTSCOPE_TAG="$TARGET_TAG"
START="$(date -u +%s)"

# Only the app services are recreated. Restarting Postgres/Redis would add an
# unnecessary data-layer outage to what should be a code-only change.
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate api worker beat

API_PORT="${API_HOST_PORT:-8000}"
echo "  verifying /ready on localhost:${API_PORT}"
READY=false
for _ in $(seq 1 60); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:${API_PORT}/ready")" = "200" ]; then
    READY=true; break
  fi
  sleep 2
done
ELAPSED=$(( $(date -u +%s) - START ))

if [ "$READY" != true ]; then
  echo "" >&2
  echo "ERROR: /ready did not return 200 after rolling back to ${TARGET_TAG} (${ELAPSED}s)." >&2
  echo "The problem is probably not the image. Check:" >&2
  echo "  - alembic state: was ${CURRENT_HEAD}; a forward migration is not undone by a rollback" >&2
  echo "  - docker compose -f ${COMPOSE_FILE} logs api" >&2
  exit 1
fi

python3 - "$STATE_FILE" "$TARGET_TAG" "$CURRENT_TAG" <<'PY'
import sys, datetime, pathlib
path, target, previous = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
lines = {}
for line in p.read_text().splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        lines[k] = v
lines["CURRENT_TAG"] = target
# The tag just rolled away from becomes the rollback target, so a bad rollback
# can itself be rolled back.
lines["PREVIOUS_TAG"] = previous
lines["DEPLOYED_AT"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
lines["ROLLED_BACK"] = "true"
p.write_text("\n".join(f"{k}={v}" for k, v in lines.items()) + "\n")
PY
chmod 600 "$STATE_FILE"

echo ""
echo "Rolled back to talentscope:${TARGET_TAG} in ${ELAPSED}s"
curl -s "http://localhost:${API_PORT}/ready"; echo
echo ""
echo "NOTE: the database was not touched. Alembic was at: ${CURRENT_HEAD}"
echo "If the bad version ran a migration, this rollback did not undo it —"
echo "that needs scripts/restore_db.sh, not this script."
