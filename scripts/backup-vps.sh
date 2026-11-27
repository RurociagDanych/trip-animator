#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--dry-run]" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/infra/compose.yaml}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"

if [[ -f "${ENV_FILE}" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" =~ ^[[:space:]]*# ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    case "${key}" in
      POSTGRES_DB|POSTGRES_USER|BACKUP_DIR|BACKUP_RETENTION_DAYS|BACKUP_S3_URI|BACKUP_S3_ENDPOINT_URL)
        if [[ -z "${!key:-}" ]]; then
          export "${key}=${val}"
        fi
        ;;
    esac
  done < "${ENV_FILE}"
fi

BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_S3_URI="${BACKUP_S3_URI:-}"
BACKUP_S3_ENDPOINT_URL="${BACKUP_S3_ENDPOINT_URL:-}"
POSTGRES_DB="${POSTGRES_DB:-trip_animator}"
POSTGRES_USER="${POSTGRES_USER:-trip_app}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="${BACKUP_DIR}/trip-animator-${POSTGRES_DB}-${TIMESTAMP}.dump"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "dry_run=true compose_file=${COMPOSE_FILE} db=${POSTGRES_DB} user=${POSTGRES_USER} backup_file=${BACKUP_FILE} retention_days=${BACKUP_RETENTION_DAYS} offsite=${BACKUP_S3_URI:-none}"
  exit 0
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

TMP_FILE="${BACKUP_FILE}.tmp"
trap 'rm -f "${TMP_FILE}"' EXIT

docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" exec -T postgres \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --format=custom --no-owner --no-privileges \
  > "${TMP_FILE}"

if [[ ! -s "${TMP_FILE}" ]]; then
  echo "Backup failed: empty pg_dump output" >&2
  exit 1
fi

chmod 600 "${TMP_FILE}"
mv "${TMP_FILE}" "${BACKUP_FILE}"
trap - EXIT

if [[ -n "${BACKUP_S3_URI}" ]]; then
  DEST_URI="${BACKUP_S3_URI%/}/$(basename "${BACKUP_FILE}")"
  if command -v aws >/dev/null 2>&1; then
    if [[ -n "${BACKUP_S3_ENDPOINT_URL}" ]]; then
      aws --endpoint-url "${BACKUP_S3_ENDPOINT_URL}" s3 cp "${BACKUP_FILE}" "${DEST_URI}" --only-show-errors
    else
      aws s3 cp "${BACKUP_FILE}" "${DEST_URI}" --only-show-errors
    fi
  elif command -v rclone >/dev/null 2>&1; then
    rclone copyto "${BACKUP_FILE}" "${DEST_URI}"
  else
    echo "BACKUP_S3_URI is set but neither aws nor rclone is installed" >&2
    exit 1
  fi
fi

find "${BACKUP_DIR}" -maxdepth 1 -type f -name "trip-animator-${POSTGRES_DB}-*.dump" -mtime "+${BACKUP_RETENTION_DAYS}" -delete

echo "Backup completed: ${BACKUP_FILE}"
