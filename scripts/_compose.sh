# Sourced by the operational scripts. Defines dc() — "docker compose against
# whichever stack is actually running".
#
# There are two stacks: docker-compose.yml (dev, project "talentscope") and
# docker-compose.deploy.yml (project "talentscope-deploy"). The deploy file
# deliberately refuses to interpolate without TALENTSCOPE_TAG,
# POSTGRES_PASSWORD and GRAFANA_ADMIN_PASSWORD, so every command against it
# needs those loaded from .deploy-state. Without this helper, backup and
# restore would silently operate on the dev database while the deployment ran
# untouched — the worst possible way for a restore drill to "succeed".
_TS_STATE_FILE="${_TS_STATE_FILE:-.deploy-state}"

if [ -f "$_TS_STATE_FILE" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in
      CURRENT_TAG) export TALENTSCOPE_TAG="$_v" ;;
      POSTGRES_PASSWORD|GRAFANA_ADMIN_PASSWORD) export "$_k=$_v" ;;
    esac
  done < "$_TS_STATE_FILE"
  export GROQ_API_KEY="${GROQ_API_KEY:-$(grep -E '^GROQ_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)}"
  _TS_COMPOSE_ARGS=(-f docker-compose.deploy.yml)
  TS_STACK="deploy"
else
  _TS_COMPOSE_ARGS=()
  TS_STACK="dev"
fi

dc() { docker compose "${_TS_COMPOSE_ARGS[@]}" "$@"; }
