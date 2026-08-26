#!/usr/bin/env bash
# Deploys TalentScope to the current kubectl context (a kind cluster for
# local dev — see k8s/kind-config.yaml). Not meant for a real cluster
# as-is: 02-secret.example.yaml is a template, and this script generates
# the real Secret from local .env, which is fine for a disposable local
# kind cluster but is exactly where a real deployment would swap in an
# External Secrets Operator instead (see docs/kubernetes.md).
set -euo pipefail
cd "$(dirname "$0")/.."

kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-configmap.yaml

kubectl create configmap target-companies \
  --from-file=target_companies.yml=config/target_companies.yml \
  -n talentscope --dry-run=client -o yaml | kubectl apply -f -

if [ -f .env ]; then
  # shellcheck disable=SC1091
  source .env
fi
kubectl create secret generic talentscope-secrets \
  --from-literal=GROQ_API_KEY="${GROQ_API_KEY:-}" \
  --from-literal=ADZUNA_APP_ID="${ADZUNA_APP_ID:-}" \
  --from-literal=ADZUNA_APP_KEY="${ADZUNA_APP_KEY:-}" \
  --from-literal=POSTGRES_PASSWORD="talentscope" \
  -n talentscope --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/10-postgres.yaml
kubectl apply -f k8s/11-redis.yaml

echo "Waiting for postgres + redis..."
kubectl rollout status statefulset/postgres -n talentscope --timeout=120s
kubectl rollout status deployment/redis -n talentscope --timeout=120s

kubectl delete job/migrate -n talentscope --ignore-not-found
kubectl apply -f k8s/12-migrate-job.yaml
echo "Waiting for migration job..."
kubectl wait --for=condition=complete job/migrate -n talentscope --timeout=180s

kubectl apply -f k8s/20-api.yaml
kubectl apply -f k8s/21-worker.yaml
kubectl apply -f k8s/22-beat.yaml

echo "Waiting for api + worker + beat..."
kubectl rollout status deployment/api -n talentscope --timeout=180s
kubectl rollout status deployment/worker -n talentscope --timeout=180s
kubectl rollout status deployment/beat -n talentscope --timeout=120s

echo "Done. kubectl get pods -n talentscope"
