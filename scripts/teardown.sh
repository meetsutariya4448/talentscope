#!/usr/bin/env bash
# Remove everything this exercise created. Idempotent; --dry-run first.
#
#   bash scripts/teardown.sh --dry-run     # show what would be removed
#   bash scripts/teardown.sh --yes         # do it
#   bash scripts/teardown.sh --yes --keep-data   # leave volumes alone
#
# There was no teardown path at all before this — not for compose volumes, not
# for the kind cluster, not for Terraform. An operating exercise that cannot be
# torn down is not temporary, it is just abandoned, and abandoned infrastructure
# is what generates surprise bills on a real account.
#
# Order matters: the app stack first (so nothing is still writing), then
# Terraform (so it does not fight the LocalStack container it talks to), then
# LocalStack itself, then local images and files.
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=true
KEEP_DATA=false
KEEP_IMAGES=false

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --yes|-y)  DRY_RUN=false; shift ;;
    --keep-data) KEEP_DATA=true; shift ;;
    --keep-images) KEEP_IMAGES=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

run() {
  if [ "$DRY_RUN" = true ]; then
    echo "    would run: $*"
  else
    echo "    + $*"
    "$@" || echo "      (non-fatal: command failed, continuing)"
  fi
}

if [ "$DRY_RUN" = true ]; then
  echo "=== TEARDOWN (dry run — nothing will be removed) ==="
else
  echo "=== TEARDOWN ==="
fi

VOLUME_FLAG=()
if [ "$KEEP_DATA" = true ]; then
  echo ""
  echo "  --keep-data: named volumes (database, Prometheus, Grafana, traces) will be KEPT"
else
  VOLUME_FLAG=(-v)
fi

# --- 1. Application stacks -------------------------------------------------
echo ""
echo "[1/6] Compose stacks"
for f in docker-compose.deploy.yml docker-compose.yml; do
  [ -f "$f" ] || continue
  # TALENTSCOPE_TAG etc. are required by the deploy file's variable
  # interpolation even to parse it for `down`; the values are irrelevant here.
  if [ "$DRY_RUN" = true ]; then
    echo "    would run: docker compose -f $f down ${VOLUME_FLAG[*]} --remove-orphans"
  else
    echo "    + docker compose -f $f down ${VOLUME_FLAG[*]} --remove-orphans"
    TALENTSCOPE_TAG="${TALENTSCOPE_TAG:-teardown}" \
    POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-teardown}" \
    GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-teardown}" \
      docker compose -f "$f" down "${VOLUME_FLAG[@]}" --remove-orphans \
      || echo "      (non-fatal: compose down failed, continuing)"
  fi
done

# Containers started outside compose during the exercise (the proxy demo).
for c in talentscope-api2-1 talentscope-nginx-1; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    run docker rm -f "$c"
  fi
done

# --- 2. Terraform ----------------------------------------------------------
echo ""
echo "[2/6] Terraform"
if [ -f terraform/terraform.tfstate ] && [ -s terraform/terraform.tfstate ]; then
  COUNT="$(python3 -c "
import json
try:
    d=json.load(open('terraform/terraform.tfstate'))
    print(sum(len(r.get('instances',[])) for r in d.get('resources',[]) if r.get('mode')=='managed'))
except Exception:
    print(0)" 2>/dev/null || echo 0)"
  echo "    ${COUNT} managed resources in state"
  if [ "$COUNT" != "0" ]; then
    if [ "$DRY_RUN" = true ]; then
      echo "    would run: terraform -chdir=terraform destroy -auto-approve"
    else
      echo "    + terraform -chdir=terraform destroy -auto-approve"
      # Destroy needs LocalStack still reachable — hence this step comes
      # before stopping it.
      TF_VAR_groq_api_key="${TF_VAR_groq_api_key:-placeholder}" \
        terraform -chdir=terraform destroy -auto-approve \
        || echo "      (non-fatal: destroy failed — check LocalStack is still up)"
    fi
  fi
else
  echo "    no Terraform state"
fi

# --- 3. LocalStack ---------------------------------------------------------
echo ""
echo "[3/6] LocalStack"
if docker ps -a --format '{{.Names}}' | grep -qx talentscope-localstack; then
  run docker rm -f talentscope-localstack
else
  echo "    not running"
fi

# --- 4. kind ---------------------------------------------------------------
echo ""
echo "[4/6] kind cluster"
if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx talentscope; then
  run kind delete cluster --name talentscope
else
  echo "    no 'talentscope' kind cluster"
fi

# --- 5. Images -------------------------------------------------------------
echo ""
echo "[5/6] Images"
if [ "$KEEP_IMAGES" = true ]; then
  echo "    --keep-images: skipping"
else
  TAGS="$(docker images talentscope --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -v '<none>' || true)"
  if [ -n "$TAGS" ]; then
    echo "$TAGS" | while read -r img; do run docker rmi "$img"; done
  else
    echo "    no talentscope images"
  fi
  # The compose-built dev image is named after the project, not 'talentscope'.
  if docker images talentscope-api --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -q .; then
    run docker rmi talentscope-api:latest
  fi
fi

# --- 6. Local files --------------------------------------------------------
echo ""
echo "[6/6] Local files"
# .deploy-state holds generated passwords; local dumps hold real data. Both are
# gitignored, and neither should outlive the stack they belong to.
for f in .deploy-state; do
  [ -e "$f" ] && run rm -f "$f"
done
if [ "$KEEP_DATA" = false ] && [ -d backups ]; then
  echo "    backups/ contains $(ls -1 backups 2>/dev/null | wc -l | tr -d ' ') file(s)"
  run rm -rf backups
fi

echo ""
if [ "$DRY_RUN" = true ]; then
  echo "Dry run complete. Re-run with --yes to actually remove."
else
  echo "Teardown complete."
  echo ""
  echo "Remaining (intentionally): the git repository, evals/ artifacts,"
  echo "docs/ and the incident write-ups. Those are the output of the"
  echo "exercise, not part of the running system."
fi
