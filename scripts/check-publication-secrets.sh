#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v gitleaks >/dev/null 2>&1; then
  printf 'Required command not found: gitleaks\n' >&2
  exit 1
fi
if [[ "$(gitleaks version)" != "8.30.1" ]]; then
  printf 'Expected Gitleaks 8.30.1.\n' >&2
  exit 1
fi
if [[ ! -f .gitleaks.toml ]]; then
  printf 'Required scanner config .gitleaks.toml is missing.\n' >&2
  exit 1
fi

if [[ "$(git rev-parse --is-shallow-repository)" != false ]]; then
  printf 'Refusing publication scan: a complete Git checkout is required.\n' >&2
  exit 1
fi
if [[ -e .gitleaksignore ]]; then
  printf 'Refusing publication scan: use reviewed, narrow .gitleaks.toml exceptions instead of .gitleaksignore.\n' >&2
  exit 1
fi

scratch_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
source_snapshot="$(mktemp -d "$scratch_root/trip-publication-source.XXXXXX")"
paths_file="$(mktemp "$scratch_root/trip-publication-paths.XXXXXX")"
trap 'rm -rf -- "$source_snapshot"; rm -f -- "$paths_file"' EXIT

# Copy only Git-visible source into a temporary tree. Never feed local .env,
# credential/key files, or ignored runtime/dependency output to the directory scan.
git ls-files --cached --others --exclude-standard -z > "$paths_file"
while IFS= read -r -d '' path; do
  case "/$path/" in
    */.venv/*|*/venv/*|*/node_modules/*|*/__pycache__/*|*/.pytest_cache/*|*/.mypy_cache/*|*/.ruff_cache/*|*/.next/*|*/dist/*|*/build/*|*/coverage/*|*/.terraform/*|*/target/*|*/data/*|*/runtime/*|*/artifacts/*|*/logs/*)
      printf 'Refusing publication: a runtime/generated path is Git-visible.\n' >&2
      exit 1
      ;;
  esac
  case "$(basename -- "$path")" in
    .env|.env.*)
      if [[ "$path" != .env.example ]]; then
        printf 'Refusing publication: a non-example environment file is Git-visible.\n' >&2
        exit 1
      fi
      ;;
    *credential*|*credentials*|id_rsa|id_ed25519|*.pem|*.key|*.p12|*.pfx)
      printf 'Refusing publication: a credential/key path is Git-visible.\n' >&2
      exit 1
      ;;
  esac
  if [[ -L "$path" ]]; then
    printf 'Refusing publication: review Git-visible symbolic links explicitly.\n' >&2
    exit 1
  fi
  # History covers HEAD, not the uncommitted index. Never approve a different
  # staged blob using only its clean (or missing) working-tree counterpart.
  if ! git diff --cached --quiet -- "$path"; then
    if [[ ! -f "$path" ]] || ! git cat-file blob ":$path" | cmp -s -- "$path" -; then
      printf 'Refusing publication: staged content differs from the scanned working tree.\n' >&2
      exit 1
    fi
  fi
  [[ ! -f "$path" ]] && continue
  mkdir -p -- "$source_snapshot/$(dirname -- "$path")"
  cp -- "$path" "$source_snapshot/$path"
done < "$paths_file"

printf 'Scanning complete Git history with Gitleaks 8.30.1...\n'
gitleaks git --log-opts="--all --reflog" --config "$ROOT_DIR/.gitleaks.toml" --redact=100 --ignore-gitleaks-allow --no-banner --no-color --log-level error "$ROOT_DIR"

printf 'Scanning filtered current-source snapshot with Gitleaks 8.30.1...\n'
gitleaks dir --config "$ROOT_DIR/.gitleaks.toml" --redact=100 --ignore-gitleaks-allow --no-banner --no-color --log-level error "$source_snapshot"

printf 'Publication history and source scans passed.\n'
