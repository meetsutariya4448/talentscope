#!/usr/bin/env bash
# Deploy the stack at an explicit, immutable image tag.
#
#   bash scripts/deploy.sh                 # deploy the current commit
#   bash scripts/deploy.sh a1b2c3d         # deploy a specific tag
#   bash scripts/deploy.sh --build-only    # build and tag, don't deploy
#
# The tag is a git SHA, never `latest`. Everything before this deployed
# `talentscope:latest`, which makes rollback impossible by construction: there
# is no name for "the version that was working ten minutes ago". The tag is
# also recorded in .deploy-state so scripts/rollback.sh knows what to go back
# to without the operator having to remember.
#
# Secrets come from Secrets Manager (LocalStack by default), not from a file in
# the repo — the same path terraform/compute.tf's cloud-init uses on a real
# instance, so the mechanism is exercised rather than described.
set -euo pipefail
cd "$(dirname "$0")/.."

STATE_FILE=".deploy-state"
COMPOSE_FILE="docker-compose.deploy.yml"
AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL-http://localhost:4566}"
SECRET_ID="${SECRET_ID:-talentscope-dev/app}"
BUILD_ONLY=false
TAG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --build-only) BUILD_ONLY=true; shift ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) TAG="$1"; shift ;;
  esac
done

if [ -z "$TAG" ]; then
  TAG="$(git rev-parse --short HEAD)"
  if [ -n "$(git status --porcelain)" ]; then
    # A tag naming a commit whose tree you did not actually deploy is worse
    # than no tag: a later rollback to it would restore something that never
    # ran. A bare "-dirty" suffix is not enough either — it is not
    # content-addressed, so two different working trees share one tag, the
    # "image already present" check below skips the rebuild, and you silently
    # redeploy the old code. Hash the actual uncommitted content instead.
    DIRTY_HASH="$( { git diff HEAD; \
                     git ls-files --others --exclude-standard \
                       | while read -r f; do printf '%s ' "$f"; cat "$f" 2>/dev/null; done; \
                   } | shasum | cut -c1-8 )"
    TAG="${TAG}-dirty.${DIRTY_HASH}"
    echo "WARNING: working tree is dirty; tagging as ${TAG}"
  fi
fi

echo "=== Deploying talentscope:${TAG} ==="

# --- 1. Build ---
if ! docker image inspect "talentscope:${TAG}" >/dev/null 2>&1; then
  echo "  building talentscope:${TAG}"
  docker build -t "talentscope:${TAG}" .
else
  echo "  image talentscope:${TAG} already present, not rebuilding"
fi

if [ "$BUILD_ONLY" = true ]; then
  echo "  --build-only: stopping here"
  exit 0
fi

# --- 2. Secrets ---
if [ -n "$AWS_ENDPOINT_URL" ]; then
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
  export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  export AWS_REQUEST_CHECKSUM_CALCULATION="${AWS_REQUEST_CHECKSUM_CALCULATION:-when_required}"
  export AWS_RESPONSE_CHECKSUM_VALIDATION="${AWS_RESPONSE_CHECKSUM_VALIDATION:-when_required}"
  AWS_ARGS=(--endpoint-url "$AWS_ENDPOINT_URL")
else
  AWS_ARGS=()
fi

echo "  reading secrets from Secrets Manager (${SECRET_ID})"
SECRET_JSON="$(aws "${AWS_ARGS[@]}" secretsmanager get-secret-value \
  --secret-id "$SECRET_ID" --query SecretString --output text 2>/dev/null || echo '')"

if [ -z "$SECRET_JSON" ]; then
  echo "  WARNING: could not read ${SECRET_ID}; falling back to the local .env"
  SECRET_JSON='{}'
fi

export GROQ_API_KEY="$(printf '%s' "$SECRET_JSON" | python3 -c \
  'import json,sys
try: print(json.load(sys.stdin).get("GROQ_API_KEY") or "")
except Exception: print("")')"
[ -n "$GROQ_API_KEY" ] || GROQ_API_KEY="$(grep -E '^GROQ_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)"
export GROQ_API_KEY

# Generated once and persisted, never a committed literal. The old default was
# the string `talentscope`, hardcoded in five files.
if [ -f "$STATE_FILE" ] && grep -q '^POSTGRES_PASSWORD=' "$STATE_FILE"; then
  POSTGRES_PASSWORD="$(grep '^POSTGRES_PASSWORD=' "$STATE_FILE" | cut -d= -f2-)"
else
  POSTGRES_PASSWORD="$(openssl rand -hex 24)"
  echo "  generated a new POSTGRES_PASSWORD"
fi
export POSTGRES_PASSWORD

if [ -f "$STATE_FILE" ] && grep -q '^GRAFANA_ADMIN_PASSWORD=' "$STATE_FILE"; then
  GRAFANA_ADMIN_PASSWORD="$(grep '^GRAFANA_ADMIN_PASSWORD=' "$STATE_FILE" | cut -d= -f2-)"
else
  GRAFANA_ADMIN_PASSWORD="$(openssl rand -hex 16)"
  echo "  generated a new GRAFANA_ADMIN_PASSWORD"
fi
export GRAFANA_ADMIN_PASSWORD

# The otel collector runs as uid 10001 and writes traces here. Created on the
# host so the directory exists with usable ownership before the bind mount.
mkdir -p observability/traces
chmod 777 observability/traces 2>/dev/null || true

# --- 3. Record the outgoing tag before replacing it ---
PREVIOUS_TAG=""
[ -f "$STATE_FILE" ] && PREVIOUS_TAG="$(grep '^CURRENT_TAG=' "$STATE_FILE" 2>/dev/null | cut -d= -f2- || true)"

# --- 4. Deploy ---
export TALENTSCOPE_TAG="$TAG"
echo "  starting stack (previous tag: ${PREVIOUS_TAG:-none})"
docker compose -f "$COMPOSE_FILE" up -d --wait --wait-timeout 180 || {
  echo ""
  echo "ERROR: stack did not become healthy." >&2
  docker compose -f "$COMPOSE_FILE" ps >&2
  echo "" >&2
  echo "Roll back with: bash scripts/rollback.sh" >&2
  exit 1
}

# --- 5. Verify readiness rather than trusting `up` ---
API_PORT="${API_HOST_PORT:-8000}"
echo "  verifying /ready on localhost:${API_PORT}"
READY=false
for _ in $(seq 1 60); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:${API_PORT}/ready")" = "200" ]; then
    READY=true; break
  fi
  sleep 2
done

if [ "$READY" != true ]; then
  echo "ERROR: /ready never returned 200." >&2
  curl -s "http://localhost:${API_PORT}/ready" >&2 || true
  echo "" >&2
  echo "Roll back with: bash scripts/rollback.sh" >&2
  exit 1
fi

# --- 6. Persist state for rollback ---
{
  echo "CURRENT_TAG=${TAG}"
  [ -n "$PREVIOUS_TAG" ] && [ "$PREVIOUS_TAG" != "$TAG" ] && echo "PREVIOUS_TAG=${PREVIOUS_TAG}"
  echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
  echo "GRAFANA_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}"
  echo "DEPLOYED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$STATE_FILE"
chmod 600 "$STATE_FILE"

echo ""
echo "Deployed talentscope:${TAG}"
curl -s "http://localhost:${API_PORT}/ready"; echo
echo "  api          http://localhost:${API_PORT}"
echo "  prometheus   http://localhost:${PROMETHEUS_HOST_PORT:-9090}"
echo "  alertmanager http://localhost:${ALERTMANAGER_HOST_PORT:-9093}"
echo "  grafana      http://localhost:${GRAFANA_HOST_PORT:-3000} (admin / see ${STATE_FILE})"
[ -n "$PREVIOUS_TAG" ] && echo "  rollback to  ${PREVIOUS_TAG}:  bash scripts/rollback.sh"
exit 0
