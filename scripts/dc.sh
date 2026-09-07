#!/usr/bin/env bash
# Thin wrapper: `docker compose` against the deployed stack, with the
# generated credentials loaded from .deploy-state.
#
#   bash scripts/dc.sh ps
#   bash scripts/dc.sh logs api --tail 50
#   bash scripts/dc.sh exec -T api python scripts/seed_demo.py --embed
#
# docker-compose.deploy.yml requires TALENTSCOPE_TAG, POSTGRES_PASSWORD and
# GRAFANA_ADMIN_PASSWORD to even interpolate — that strictness is deliberate
# (an unset tag must fail loudly rather than resolve to something arbitrary),
# but it makes every ad-hoc compose command against a running deployment
# awkward. This supplies them from the state file scripts/deploy.sh wrote,
# so the values still never live in a committed file.
set -euo pipefail
cd "$(dirname "$0")/.."

STATE_FILE=".deploy-state"
[ -f "$STATE_FILE" ] || {
  echo "No ${STATE_FILE} — nothing deployed. Run: bash scripts/deploy.sh" >&2
  exit 1
}

# shellcheck disable=SC2046
export $(grep -E '^(CURRENT_TAG|POSTGRES_PASSWORD|GRAFANA_ADMIN_PASSWORD)=' "$STATE_FILE" | xargs -0 echo | tr '\n' ' ') >/dev/null 2>&1 || true
while IFS='=' read -r k v; do
  case "$k" in
    CURRENT_TAG) export TALENTSCOPE_TAG="$v" ;;
    POSTGRES_PASSWORD|GRAFANA_ADMIN_PASSWORD) export "$k=$v" ;;
  esac
done < "$STATE_FILE"

export GROQ_API_KEY="${GROQ_API_KEY:-$(grep -E '^GROQ_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)}"

exec docker compose -f docker-compose.deploy.yml "$@"
