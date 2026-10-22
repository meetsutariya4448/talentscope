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

KEY=""; LATEST=false; LIST=false; ASSUME_YES=false; SELECTIONS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --key)
      [ "$#" -ge 2 ] || { echo "--key requires a backup name" >&2; exit 2; }
      KEY="$2"; SELECTIONS=$((SELECTIONS + 1)); shift 2
      ;;
    --latest) LATEST=true; SELECTIONS=$((SELECTIONS + 1)); shift ;;
    --list)   LIST=true; SELECTIONS=$((SELECTIONS + 1)); shift ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ "$SELECTIONS" -gt 1 ]; then
  echo "choose exactly one of --latest, --key NAME, or --list" >&2
  exit 2
fi
for identifier in "$PGDATABASE" "$PGUSER"; do
  if [[ ! "$identifier" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [ "${#identifier}" -gt 63 ]; then
    echo "PGDATABASE and PGUSER must be PostgreSQL identifiers of at most 63 characters" >&2
    exit 2
  fi
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
    | awk '$4 ~ /\.dump\.gz$/ { print }' | sort
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

# backup_db.sh writes flat, timestamped object names. Refuse path-like or
# option-like keys before using the value as both an S3 key and local path.
# Besides preventing traversal out of LOCAL_DIR, this keeps later tools from
# interpreting an attacker-controlled basename as an option.
case "$KEY" in
  -*|*/*|*\\*|*..*|*[!A-Za-z0-9._-]*)
    echo "Invalid backup key '${KEY}'; expected a flat *.dump.gz filename." >&2
    exit 2
    ;;
esac
case "$KEY" in
  *.dump.gz) ;;
  *)
    echo "Invalid backup key '${KEY}'; expected a flat *.dump.gz filename." >&2
    exit 2
    ;;
esac

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
MANIFEST_FILE="${LOCAL_FILE}.manifest.json"
if [ -e "$LOCAL_FILE" ] || [ -e "$MANIFEST_FILE" ]; then
  echo "Refusing to overwrite existing local recovery evidence for ${KEY}." >&2
  exit 1
fi
echo "  downloading s3://${BACKUP_BUCKET}/${KEY}"
aws "${AWS_ARGS[@]}" s3 cp "s3://${BACKUP_BUCKET}/${KEY}" "$LOCAL_FILE" --only-show-errors

# Validate the archive before terminating sessions or dropping the database.
# A truncated S3 object must fail while the existing database is still intact,
# not halfway through the destructive restore sequence.
if ! gzip -t "$LOCAL_FILE" 2>/dev/null; then
  echo "Downloaded backup is not a valid gzip archive; database was not changed." >&2
  exit 1
fi

EXPECTED_COUNTS=""
if aws "${AWS_ARGS[@]}" s3 cp "s3://${BACKUP_BUCKET}/${KEY}.manifest.json" \
     "$MANIFEST_FILE" --only-show-errors 2>/dev/null; then
  BACKUP_BYTES="$(wc -c < "$LOCAL_FILE" | tr -d ' ')"
  if ! EXPECTED_COUNTS="$(python3 - "$MANIFEST_FILE" "$KEY" "$PGDATABASE" "$BACKUP_BYTES" <<'PY'
import json
import re
import sys

manifest_path, expected_key, expected_database, expected_bytes = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as manifest_file:
    manifest = json.load(manifest_file)
if not isinstance(manifest, dict):
    raise ValueError("manifest root must be an object")
if manifest.get("backup") != expected_key:
    raise ValueError("manifest backup name does not match the selected object")
if manifest.get("database") != expected_database:
    raise ValueError("manifest database does not match the restore target")
if isinstance(manifest.get("bytes"), bool) or manifest.get("bytes") != int(expected_bytes):
    raise ValueError("manifest byte count does not match the downloaded backup")
row_counts = manifest.get("row_counts")
if not isinstance(row_counts, str) or not re.fullmatch(
    r"postings=\d+ companies=\d+ skills=\d+ embedded=\d+", row_counts
):
    raise ValueError("manifest row_counts is invalid")
print(row_counts)
PY
  )"; then
    echo "Downloaded backup manifest is invalid; database was not changed." >&2
    exit 1
  fi
  echo "  manifest says: ${EXPECTED_COUNTS}"
else
  echo "  (no manifest alongside this backup — cannot verify row counts)"
fi

# gzip integrity only proves that the compression stream is intact. Ask
# pg_restore to parse the custom-format archive before terminating sessions or
# dropping the existing database, so a valid gzip containing unrelated bytes
# fails while recovery is still non-destructive.
if ! gunzip -c "$LOCAL_FILE" \
  | dc exec -T postgres pg_restore --list >/dev/null; then
  echo "Downloaded backup is not a readable pg_dump archive; database was not changed." >&2
  exit 1
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
