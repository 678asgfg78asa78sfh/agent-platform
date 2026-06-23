#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./update.sh
  ./update.sh /path/to/existing/agent-install

Run this script from the freshly extracted update ZIP. It copies the new
application files into an existing install while preserving customer state:
  - agent-data/
  - modules/*/config.json
  - .env files
  - docker-compose.yml
  - docker-compose.override.yml

The target install directory is the folder that contains agent-data/config.json.
If no target path is given, the script tries to find the currently running
agent process and updates that exact installation.

Options:
  --target PATH   Existing install directory, same as positional target
  --dry-run       Show what would be copied
  --no-build      Do not run cargo build and do not copy a bundled binary
  -h, --help      Show this help
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "==> $*"
}

detect_running_target() {
  local pid exe dir parent
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    [[ -n "$exe" ]] || continue
    case "$exe" in
      */target/release/agent)
        dir="${exe%/target/release/agent}"
        ;;
      */agent)
        dir="$(cd -- "$(dirname -- "$exe")" && pwd)"
        parent="$(cd -- "$dir/.." && pwd)"
        if [[ -f "$parent/agent-data/config.json" || -f "$parent/Cargo.toml" ]]; then
          dir="$parent"
        fi
        ;;
      *)
        continue
        ;;
    esac
    if [[ -f "$dir/agent-data/config.json" || -f "$dir/Cargo.toml" ]]; then
      printf '%s\n' "$dir"
      return 0
    fi
  done < <(pgrep -x agent 2>/dev/null || true)
  return 1
}

show_install_candidates() {
  local base
  for base in "$PWD" "${HOME:-}" /opt /srv; do
    [[ -n "$base" && -d "$base" ]] || continue
    find "$base" -maxdepth 5 -type f -path "*/agent-data/config.json" -printf '%h\n' 2>/dev/null \
      | sed 's#/agent-data$##'
  done | awk '!seen[$0]++' | sed 's#^#  #'
}

TARGET=""
DRY_RUN=0
NO_BUILD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || die "--target needs a path"
      TARGET="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --no-build)
      NO_BUILD=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      die "Unknown option: $1"
      ;;
    *)
      [[ -z "$TARGET" ]] || die "Target given twice"
      TARGET="$1"
      shift
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR"

[[ -f "$SOURCE_DIR/Cargo.toml" ]] || die "Source ZIP root not detected: Cargo.toml missing in $SOURCE_DIR"
[[ -d "$SOURCE_DIR/src" ]] || die "Source ZIP root not detected: src/ missing in $SOURCE_DIR"

if [[ -z "$TARGET" ]]; then
  if TARGET="$(detect_running_target)"; then
    info "Auto-detected running Agent install: $TARGET"
  else
    echo "ERROR: Missing target install directory and no running agent process was detected." >&2
    echo "The target is the folder containing agent-data/config.json." >&2
    echo "Candidates found:" >&2
    show_install_candidates >&2 || true
    exit 1
  fi
fi

[[ -d "$TARGET" ]] || die "Target directory does not exist: $TARGET"
TARGET_DIR="$(cd -- "$TARGET" && pwd)"

if [[ "$SOURCE_DIR" == "$TARGET_DIR" ]]; then
  die "Source and target are the same directory. Extract the new ZIP elsewhere, then run: ./update.sh $TARGET_DIR"
fi

if [[ -e "$TARGET_DIR/Cargo.toml" && ! -d "$TARGET_DIR/src" ]]; then
  die "Target looks incomplete: Cargo.toml exists but src/ is missing"
fi

if [[ ! -e "$TARGET_DIR/Cargo.toml" && ! -d "$TARGET_DIR/agent-data" ]]; then
  die "Target does not look like an Agent install: expected Cargo.toml or agent-data/"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$TARGET_DIR/.update-backups/$STAMP"
mkdir -p "$BACKUP_DIR"

backup_paths=()
while IFS= read -r -d '' path; do
  rel="${path#$TARGET_DIR/}"
  backup_paths+=("$rel")
done < <(
  find "$TARGET_DIR" \
    \( -path "$TARGET_DIR/.update-backups" -o -path "$TARGET_DIR/.git" \) -prune -o \
    \( \
      -path "$TARGET_DIR/agent-data/config.json" -o \
      -path "$TARGET_DIR/agent-data/config.json.bak-*" -o \
      -path "$TARGET_DIR/modules/*/config.json" -o \
      -name ".env" -o \
      -name ".env.*" -o \
      -name "docker-compose.override.yml" -o \
      -name "docker-compose.yml" \
    \) -type f -print0
)

if [[ ${#backup_paths[@]} -gt 0 ]]; then
  info "Backing up customer settings to $BACKUP_DIR/settings.tgz"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    tar -C "$TARGET_DIR" -czf "$BACKUP_DIR/settings.tgz" "${backup_paths[@]}"
  else
    printf 'DRY-RUN backup paths:\n'
    printf '  %s\n' "${backup_paths[@]}"
  fi
else
  info "No existing settings files found to back up"
fi

EXCLUDES=(
  ".git/"
  ".update-backups/"
  "agent-data/"
  "target/"
  "node_modules/"
  "__pycache__/"
  "*/__pycache__/"
  "agent.out"
  "*.log"
  "*.pyc"
  "*.pyo"
  ".env"
  ".env.*"
  "docker-compose.yml"
  "docker-compose.override.yml"
  "modules/*/config.json"
  "modules/*/*.sqlite"
  "modules/*/*.sqlite3"
  "modules/*/*.db"
  "modules/*/*.bak*"
  "modules/*/.*.bak*"
  "modules/*/*.migrated*"
)

info "Copying update files into $TARGET_DIR"
if command -v rsync >/dev/null 2>&1; then
  RSYNC_ARGS=(-a)
  [[ "$DRY_RUN" -eq 1 ]] && RSYNC_ARGS+=(--dry-run --itemize-changes)
  for pattern in "${EXCLUDES[@]}"; do
    RSYNC_ARGS+=(--exclude "$pattern")
  done
  rsync "${RSYNC_ARGS[@]}" "$SOURCE_DIR"/ "$TARGET_DIR"/
else
  TAR_EXCLUDES=()
  for pattern in "${EXCLUDES[@]}"; do
    clean_pattern="${pattern%/}"
    TAR_EXCLUDES+=(--exclude="./$clean_pattern")
    if [[ "$clean_pattern" != *"*"* ]]; then
      TAR_EXCLUDES+=(--exclude="./$clean_pattern/*")
    fi
  done
  if [[ "$DRY_RUN" -eq 1 ]]; then
    tar -C "$SOURCE_DIR" "${TAR_EXCLUDES[@]}" -cf - . \
      | tar -tf - \
      | awk '($0 != "." && $0 != "./") { sub("^\\./", "DRY-RUN copy: "); print }'
  else
    tar -C "$SOURCE_DIR" "${TAR_EXCLUDES[@]}" -cf - . | tar -C "$TARGET_DIR" -xf -
  fi
fi

if [[ "$NO_BUILD" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
  if [[ -x "$SOURCE_DIR/target/release/agent" ]]; then
    info "Copying bundled release binary"
    mkdir -p "$TARGET_DIR/target/release"
    cp -a "$SOURCE_DIR/target/release/agent" "$TARGET_DIR/target/release/agent.new"
    mv "$TARGET_DIR/target/release/agent.new" "$TARGET_DIR/target/release/agent"
  elif command -v cargo >/dev/null 2>&1; then
    info "Building release binary with cargo"
    (cd "$TARGET_DIR" && cargo build --release)
  else
    echo "WARN: cargo not found and no bundled target/release/agent binary in ZIP." >&2
    echo "WARN: Source files were updated, but the binary was not rebuilt." >&2
  fi
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  target_bin="$TARGET_DIR/target/release/agent"
  if [[ -f "$target_bin" ]]; then
    if grep -a -q "NVIDIA NIM" "$target_bin" \
      && grep -a -q "llm-context-window" "$target_bin" \
      && grep -a -q "bearer_token_value" "$target_bin" \
      && grep -a -q "context_window" "$target_bin" \
      && grep -a -q "clamping to" "$target_bin"; then
      info "Verified target binary: NVIDIA provider, token fields, Bearer-key normalizer, context max-token guard found"
    else
      echo "WARN: target binary does not contain all new NVIDIA/token/Bearer/context-guard markers." >&2
      echo "WARN: The browser will still show the old UI until target/release/agent is replaced and restarted." >&2
    fi
  else
    echo "WARN: no target binary found at $target_bin" >&2
  fi
fi

info "Update copied. Preserved runtime data in $TARGET_DIR/agent-data"
info "Restart the running agent service/process so it uses the updated binary."
echo "Backup: $BACKUP_DIR"
