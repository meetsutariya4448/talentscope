#!/usr/bin/env bash
# Restore the Postgres database from a backup in the S3 backups bucket.
#
#   bash scripts/restore_db.sh --latest
#   bash scripts/restore_db.sh --key talentscope-talentscope-2026...dump.gz
#   bash scripts/restore_db.sh --list
#
# DESTRUCTIVE: drops and recreates the target database. It refuses to run
# without --yes unless stdin is a terminal, so it cannot be triggered by
# accident from a script or a stray pipe.
#
# Verifies afterwards by comparing restored row counts against the counts the
# backup recorded in its manifest. A restore that exits 0 having restored the
# wrong or empty data is the failure this is guarding against — "pg_restore
# succeeded" is not the same as "the data is back".
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=scripts/_compose.sh
. "$(dirname "$0")/_compose.sh"

AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL-http://localhost:4566}"
BACKUP_BUCKET="${BACKUP_BUCKET:-talentscope-dev-backups}"
PGDATABASE="${PGDATABASE:-talentscope}"
PGUSER="${PGUSER:-talentscope}"
LOCAL_DIR="${LOCAL_DIR:-backups}"

KEY=""; LATEST=false; LIST=false; ASSUME_YES=false
while [ $# -gt 0 ]; do
  case "$1" in
    --key)    KEY="$2"; shift 2 ;;
    --latest) LATEST=true; shift ;;
    --list)   LIST=true; shift ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -n "$AWS_ENDPOINT_URL" ]; then
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
  export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  # See scripts/backup_db.sh for why these two are needed against LocalStack.
  export AWS_REQUEST_CHECKSUM_CALCULATION="${AWS_REQUEST_CHECKSUM_CALCULATION:-when_required}"
  export AWS_RESPONSE_CHECKSUM_VALIDATION="${AWS_RESPONSE_CHECKSUM_VALIDATION:-when_required}"
  AWS_ARGS=(--endpoint-url "$AWS_ENDPOINT_URL")
else
  AWS_ARGS=()
fi

list_backups() {
  aws "${AWS_ARGS[@]}" s3 ls "s3://${BACKUP_BUCKET}/" \
    | grep -E '\.dump\.gz$' | sort
}

if [ "$LIST" = true ]; then
  echo "Backups in s3://${BACKUP_BUCKET}/:"
  list_backups
  exit 0
fi

if [ "$LATEST" = true ]; then
  # Keys are timestamped in UTC with a fixed-width format, so lexical order is
  # chronological order.
  KEY="$(list_backups | awk '{print $4}' | tail -1)"
  [ -n "$KEY" ] || { echo "No backups found in s3://${BACKUP_BUCKET}/" >&2; exit 1; }
fi

[ -n "$KEY" ] || { echo "usage: restore_db.sh (--latest | --key NAME | --list) [--yes]" >&2; exit 2; }

echo "=== Restore ${PGDATABASE} from ${KEY} ==="

if [ "$ASSUME_YES" != true ]; then
  if [ -t 0 ]; then
    read -r -p "This DROPS and recreates database '${PGDATABASE}'. Type 'restore' to continue: " reply
    [ "$reply" = "restore" ] || { echo "aborted"; exit 1; }
  else
    echo "Refusing to run non-interactively without --yes (this drops '${PGDATABASE}')." >&2
    exit 1
  fi
fi

mkdir -p "$LOCAL_DIR"
LOCAL_FILE="${LOCAL_DIR}/${KEY}"
echo "  downloading s3://${BACKUP_BUCKET}/${KEY}"
aws "${AWS_ARGS[@]}" s3 cp "s3://${BACKUP_BUCKET}/${KEY}" "$LOCAL_FILE" --only-show-errors

EXPECTED_COUNTS=""
if aws "${AWS_ARGS[@]}" s3 cp "s3://${BACKUP_BUCKET}/${KEY}.manifest.json" \
     "${LOCAL_FILE}.manifest.json" --only-show-errors 2>/dev/null; then
  EXPECTED_COUNTS="$(python3 -c "
import json,sys
print(json.load(open('${LOCAL_FILE}.manifest.json')).get('row_counts',''))" 2>/dev/null || true)"
  echo "  manifest says: ${EXPECTED_COUNTS}"
else
  echo "  (no manifest alongside this backup — cannot verify row counts)"
fi

# Terminate other sessions first: DROP DATABASE fails while the app's
# connection pool still holds connections, and the pool reconnects instantly,
# so stopping the app is not enough on its own.
echo "  terminating existing connections"
dc exec -T postgres psql -U "$PGUSER" -d postgres -tAc "
  SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname = '${PGDATABASE}' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true

echo "  dropping and recreating ${PGDATABASE}"
dc exec -T postgres psql -U "$PGUSER" -d postgres -c \
  "DROP DATABASE IF EXISTS ${PGDATABASE};" >/dev/null
dc exec -T postgres psql -U "$PGUSER" -d postgres -c \
  "CREATE DATABASE ${PGDATABASE};" >/dev/null

# pgvector must exist before pg_restore recreates columns of type vector.
dc exec -T postgres psql -U "$PGUSER" -d "$PGDATABASE" -c \
  "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null

echo "  restoring"
# --no-owner: the dump records ownership by role name, which need not exist in
# the target. --exit-on-error so a partial restore is a failure, not a warning.
gunzip -c "$LOCAL_FILE" \
  | dc exec -T postgres pg_restore -U "$PGUSER" -d "$PGDATABASE" \
      --no-owner --exit-on-error

ACTUAL_COUNTS="$(dc exec -T postgres psql -U "$PGUSER" -d "$PGDATABASE" -tAc "
  SELECT 'postings='||(SELECT count(*) FROM postings)
      || ' companies='||(SELECT count(*) FROM companies)
      || ' skills='||(SELECT count(*) FROM skills)
      || ' embedded='||(SELECT count(*) FROM postings WHERE embedding IS NOT NULL);
" 2>/dev/null | tr -d '\r' | xargs)"

echo ""
echo "  restored: ${ACTUAL_COUNTS}"
if [ -n "$EXPECTED_COUNTS" ]; then
  echo "  expected: ${EXPECTED_COUNTS}"
  if [ "$ACTUAL_COUNTS" != "$EXPECTED_COUNTS" ]; then
    echo "MISMATCH: restored row counts differ from the backup manifest." >&2
    exit 1
  fi
  echo "  row counts match the manifest."
fi

echo ""
echo "Restore complete. The api holds pooled connections to the old database;"
echo "restart it so the pool reconnects:  docker compose restart api"
