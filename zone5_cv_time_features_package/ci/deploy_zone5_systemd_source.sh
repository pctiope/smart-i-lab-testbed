#!/usr/bin/env bash
set -euo pipefail

PRODUCTION_ROOT="${PRODUCTION_ROOT:-${ZONE5_PRODUCTION_ROOT:-$HOME/smart-i-lab-testbed}}"
PKG_REL="zone5_cv_time_features_package"
PKG_ROOT="$PRODUCTION_ROOT/$PKG_REL"
TARGET_BRANCH="${TARGET_BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
DEPLOY_SHA="${DEPLOY_SHA:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
BACKEND_HEALTH_URL="${PRODUCTION_BACKEND_HEALTH_URL:-http://192.168.10.17:8000/api/health}"
FRONTEND_HEALTH_URL="${PRODUCTION_FRONTEND_HEALTH_URL:-http://127.0.0.1:8015/api/health}"
SKIP_HEALTH_CHECKS="${SKIP_HEALTH_CHECKS:-0}"

LIVE_SERVICES=(
  zone5-person-counter.service
  zone5-mqtt-aggregator.service
  zone5-sen55-collector.service
  zone5-live-collector.service
  zone5-live-app.service
  zone5-vite-frontend.service
)

log() {
  printf '== %s ==\n' "$1"
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

disable_trainer_timer() {
  systemctl --user disable --now zone5-trainer.timer >/dev/null 2>&1 || true
}

require_no_active_trainer() {
  local state
  state="$(systemctl --user show zone5-trainer.service -p ActiveState --value 2>/dev/null || true)"
  case "$state" in
    active|activating)
      fail "zone5-trainer.service is $state. Source deploy will not run while training is active; retry after the trainer finishes."
      ;;
  esac
}

has_changed_path() {
  local query="$1"
  local changed
  while IFS= read -r changed; do
    [ "$changed" = "$query" ] && return 0
  done < "$CHANGED_FILES"
  return 1
}

has_changed_prefix() {
  local prefix="$1"
  local changed
  while IFS= read -r changed; do
    case "$changed" in
      "$prefix"*) return 0 ;;
    esac
  done < "$CHANGED_FILES"
  return 1
}

check_health() {
  local label="$1"
  local url="$2"
  local body
  local attempt
  local retries="${HEALTH_CHECK_RETRIES:-30}"
  local delay="${HEALTH_CHECK_DELAY_SECONDS:-2}"
  local last_error="health check did not run"

  log "Checking $label"
  for attempt in $(seq 1 "$retries"); do
    if body="$(curl --noproxy '*' -fsS "$url" 2>&1)" &&
      HEALTH_JSON="$body" "$PYTHON_BIN" - "$label" <<'PY'
import json
import os
import sys

label = sys.argv[1]
health = json.loads(os.environ["HEALTH_JSON"])
model_run_id = (health.get("inference") or {}).get("model_run_id")
if not health.get("ok"):
    raise SystemExit(f"{label}: health ok=false")
if not model_run_id:
    raise SystemExit(f"{label}: missing inference.model_run_id")
print(f"OK: {label} ok=true model_run_id={model_run_id}")
PY
    then
      return 0
    fi
    last_error="$body"
    if [ "$attempt" -lt "$retries" ]; then
      printf 'Waiting for %s health (%s/%s): %s\n' "$label" "$attempt" "$retries" "$last_error" >&2
      sleep "$delay"
    fi
  done
  fail "$label health check did not pass at $url after $retries attempts: $last_error"
}

require_command git
require_command systemctl
require_command curl
require_command "$PYTHON_BIN"

[ -d "$PRODUCTION_ROOT/.git" ] || fail "Production root is not a Git checkout: $PRODUCTION_ROOT"
[ -d "$PKG_ROOT" ] || fail "Missing Zone 5 package root: $PKG_ROOT"

dirty_status="$(git -C "$PRODUCTION_ROOT" status --porcelain --untracked-files=all)"
if [ -n "$dirty_status" ]; then
  printf '%s\n' "$dirty_status" >&2
  fail "Production checkout is dirty; commit, stash, or remove local changes before deploying."
fi

disable_trainer_timer
require_no_active_trainer

log "Fetching $REMOTE/$TARGET_BRANCH"
git -C "$PRODUCTION_ROOT" fetch --prune "$REMOTE" "$TARGET_BRANCH"
target_sha="${DEPLOY_SHA:-$(git -C "$PRODUCTION_ROOT" rev-parse "$REMOTE/$TARGET_BRANCH")}"
git -C "$PRODUCTION_ROOT" cat-file -e "$target_sha^{commit}" || fail "Target deploy SHA is not available locally: $target_sha"

git -C "$PRODUCTION_ROOT" checkout "$TARGET_BRANCH"
before_sha="$(git -C "$PRODUCTION_ROOT" rev-parse HEAD)"

if ! git -C "$PRODUCTION_ROOT" merge-base --is-ancestor "$before_sha" "$target_sha"; then
  fail "Production checkout cannot fast-forward from $before_sha to $target_sha."
fi

log "Fast-forwarding production checkout"
git -C "$PRODUCTION_ROOT" merge --ff-only "$target_sha"
after_sha="$(git -C "$PRODUCTION_ROOT" rev-parse HEAD)"
if [ "$after_sha" != "$target_sha" ]; then
  fail "Production checkout is at $after_sha, expected workflow SHA $target_sha."
fi

CHANGED_FILES="$(mktemp)"
trap 'rm -f "$CHANGED_FILES"' EXIT
if [ "$before_sha" != "$after_sha" ]; then
  git -C "$PRODUCTION_ROOT" diff --name-only "$before_sha" "$after_sha" > "$CHANGED_FILES"
else
  : > "$CHANGED_FILES"
fi

install -d "$PRODUCTION_ROOT/data" "$PRODUCTION_ROOT/logs" "$PRODUCTION_ROOT/stage"
install -d "$PKG_ROOT/data" "$PKG_ROOT/model" "$PKG_ROOT/logs" "$SYSTEMD_USER_DIR"

if [ ! -d "$PRODUCTION_ROOT/.python-packages" ] || has_changed_path "requirements.txt"; then
  log "Updating BSG Python package target"
  "$PYTHON_BIN" -m pip install --target "$PRODUCTION_ROOT/.python-packages" --upgrade -r "$PRODUCTION_ROOT/requirements.txt"
else
  log "BSG Python package target unchanged"
fi

if [ ! -d "$PKG_ROOT/.python-packages" ] || has_changed_path "$PKG_REL/requirements.txt"; then
  log "Updating Python package target"
  "$PYTHON_BIN" -m pip install --target "$PKG_ROOT/.python-packages" --upgrade -r "$PKG_ROOT/requirements.txt"
else
  log "Python package target unchanged"
fi

VITE_ROOT="$PKG_ROOT/web_app_vite/smart-ilab-zone5"
VITE_REL="$PKG_REL/web_app_vite/smart-ilab-zone5"
if [ -d "$VITE_ROOT" ]; then
  npm_deps_changed=0
  if [ ! -d "$VITE_ROOT/node_modules" ] ||
     has_changed_path "$VITE_REL/package.json" ||
     has_changed_path "$VITE_REL/package-lock.json"; then
    npm_deps_changed=1
  fi

  frontend_changed=0
  if has_changed_prefix "$VITE_REL/"; then
    frontend_changed=1
  fi

  if [ "$npm_deps_changed" -eq 1 ]; then
    log "Installing Vite dependencies"
    (cd "$VITE_ROOT" && npm ci)
  else
    log "Vite dependencies unchanged"
  fi

  if [ "$frontend_changed" -eq 1 ] || [ ! -d "$VITE_ROOT/dist" ]; then
    log "Building Vite frontend"
    (cd "$VITE_ROOT" && npm run build)
  else
    log "Vite build output unchanged"
  fi
fi

installed_units=0
for unit_path in "$PKG_ROOT"/systemd/user/zone5-*; do
  [ -f "$unit_path" ] || continue
  unit_name="$(basename "$unit_path")"
  unit_rel="$PKG_REL/systemd/user/$unit_name"
  unit_dest="$SYSTEMD_USER_DIR/$unit_name"
  if [ ! -f "$unit_dest" ] || has_changed_path "$unit_rel" || ! cmp -s "$unit_path" "$unit_dest"; then
    install -m 0644 "$unit_path" "$unit_dest"
    installed_units=1
    printf 'Installed user unit: %s\n' "$unit_name"
  fi
done

if [ "$installed_units" -eq 1 ]; then
  log "Reloading user systemd"
  systemctl --user daemon-reload
else
  log "User systemd units unchanged"
fi

disable_trainer_timer
require_no_active_trainer

log "Restarting Zone 5 live services"
systemctl --user reset-failed "${LIVE_SERVICES[@]}" >/dev/null 2>&1 || true
systemctl --user restart "${LIVE_SERVICES[@]}"

for service in "${LIVE_SERVICES[@]}"; do
  for _ in $(seq 1 30); do
    if systemctl --user is-active --quiet "$service"; then
      printf 'OK: %s is active\n' "$service"
      continue 2
    fi
    sleep 1
  done
  systemctl --user status "$service" --no-pager >&2 || true
  fail "$service did not become active after restart."
done

if [ "$SKIP_HEALTH_CHECKS" != "1" ]; then
  check_health "production backend" "$BACKEND_HEALTH_URL"
  check_health "production frontend proxy" "$FRONTEND_HEALTH_URL"
fi

log "Production source deploy complete"
printf 'Deployed %s to %s\n' "$after_sha" "$PRODUCTION_ROOT"
