#!/usr/bin/env bash
# Back up the Postgres database to the S3 backups bucket.
#
# pg_dump -Fc (custom format) rather than plain SQL: it is compressed, and
# pg_restore can then do a selective or parallel restore instead of replaying
# one long SQL stream. The dump runs inside the postgres container, so no
# client-side psql version has to match the server's.
#
#   bash scripts/backup_db.sh                 # -> s3://talentscope-dev-backups/
#   bash scripts/backup_db.sh --local-only    # keep the file, skip the upload
#
# Env:
#   AWS_ENDPOINT_URL   default http://localhost:4566 (LocalStack). Unset it,
#                      or point it at AWS, for a real account.
#   BACKUP_BUCKET      default talentscope-dev-backups
#   PGDATABASE         default talentscope
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=scripts/_compose.sh
. "$(dirname "$0")/_compose.sh"

AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL-http://localhost:4566}"
BACKUP_BUCKET="${BACKUP_BUCKET:-talentscope-dev-backups}"
PGDATABASE="${PGDATABASE:-talentscope}"
PGUSER="${PGUSER:-talentscope}"
LOCAL_DIR="${LOCAL_DIR:-backups}"
LOCAL_ONLY=false
[ "${1:-}" = "--local-only" ] && LOCAL_ONLY=true

# LocalStack accepts any credentials, but the AWS CLI refuses to run without
# *some* value present, so default them only when talking to LocalStack.
if [ -n "$AWS_ENDPOINT_URL" ]; then
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
  export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  # AWS CLI v2.23+ sends streaming checksums as an x-amz-trailer header by
  # default. LocalStack 3.0 rejects that outright — "The value specified in the
  # x-amz-trailer header is not supported" — so every PutObject fails and the
  # backup silently never lands. Same family of problem as the provider's
  # s3_use_path_style setting in terraform/providers.tf: real AWS is fine
  # either way, LocalStack needs the older behaviour. Scoped to the LocalStack
  # branch so a real account keeps full checksum validation.
  export AWS_REQUEST_CHECKSUM_CALCULATION="${AWS_REQUEST_CHECKSUM_CALCULATION:-when_required}"
  export AWS_RESPONSE_CHECKSUM_VALIDATION="${AWS_RESPONSE_CHECKSUM_VALIDATION:-when_required}"
  AWS_ARGS=(--endpoint-url "$AWS_ENDPOINT_URL")
else
  AWS_ARGS=()
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
NAME="talentscope-${PGDATABASE}-${TS}-${GIT_SHA}.dump.gz"
mkdir -p "$LOCAL_DIR"
DEST="${LOCAL_DIR}/${NAME}"

echo "=== Backing up ${PGDATABASE} ==="

# Row counts are captured before the dump and stored alongside it. A restore
# that completes without error but silently restores the wrong data is the
# failure mode worth guarding against, and you cannot check that after the
# fact without knowing what was there.
COUNTS="$(dc exec -T postgres psql -U "$PGUSER" -d "$PGDATABASE" -tAc "
  SELECT 'postings='||(SELECT count(*) FROM postings)
      || ' companies='||(SELECT count(*) FROM companies)
      || ' skills='||(SELECT count(*) FROM skills)
      || ' embedded='||(SELECT count(*) FROM postings WHERE embedding IS NOT NULL);
" 2>/dev/null | tr -d '\r' | xargs)"
echo "  source: $COUNTS"

dc exec -T postgres pg_dump -U "$PGUSER" -d "$PGDATABASE" -Fc \
  | gzip -9 > "$DEST"

SIZE="$(wc -c < "$DEST" | tr -d ' ')"
if [ "$SIZE" -lt 1000 ]; then
  echo "ERROR: dump is only ${SIZE} bytes — refusing to treat this as a backup." >&2
  exit 1
fi
echo "  wrote: $DEST (${SIZE} bytes)"

# A manifest next to the dump, so a restore can verify rather than assume.
MANIFEST="${DEST}.manifest.json"
cat > "$MANIFEST" <<JSON
{
  "backup": "${NAME}",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "database": "${PGDATABASE}",
  "git_sha": "${GIT_SHA}",
  "bytes": ${SIZE},
  "row_counts": "${COUNTS}",
  "format": "pg_dump -Fc | gzip -9"
}
JSON

if [ "$LOCAL_ONLY" = true ]; then
  echo "  --local-only: skipping upload"
  echo "$DEST"
  exit 0
fi

echo "  uploading to s3://${BACKUP_BUCKET}/"
aws "${AWS_ARGS[@]}" s3 cp "$DEST" "s3://${BACKUP_BUCKET}/${NAME}" --only-show-errors
aws "${AWS_ARGS[@]}" s3 cp "$MANIFEST" "s3://${BACKUP_BUCKET}/${NAME}.manifest.json" --only-show-errors

# Verify the object is actually readable back, rather than trusting the exit
# code of the upload. An unverified backup is a hope, not a backup.
REMOTE_SIZE="$(aws "${AWS_ARGS[@]}" s3api head-object \
  --bucket "$BACKUP_BUCKET" --key "$NAME" --query ContentLength --output text 2>/dev/null || echo 0)"
if [ "$REMOTE_SIZE" != "$SIZE" ]; then
  echo "ERROR: uploaded object is ${REMOTE_SIZE} bytes, expected ${SIZE}." >&2
  exit 1
fi

echo "  verified: s3://${BACKUP_BUCKET}/${NAME} (${REMOTE_SIZE} bytes)"
echo "$NAME"
