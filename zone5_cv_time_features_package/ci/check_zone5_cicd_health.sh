#!/usr/bin/env bash
set -euo pipefail

RUNNER_SERVICE="${RUNNER_SERVICE:-actions.runner.pctiope-smart-i-lab-testbed.zone5-smart-ilab-ngrg-ric.service}"
STAGING_ROOT="${STAGING_ROOT:-$HOME/smart-i-lab-testbed-compose/zone5_cv_time_features_package}"

STAGING_BACKEND_HEALTH_URL="${STAGING_BACKEND_HEALTH_URL:-http://192.168.10.17:8005/api/health}"
STAGING_FRONTEND_HEALTH_URL="${STAGING_FRONTEND_HEALTH_URL:-http://192.168.10.17:8016/api/health}"
PRODUCTION_BACKEND_HEALTH_URL="${PRODUCTION_BACKEND_HEALTH_URL:-http://192.168.10.17:8000/api/health}"
PRODUCTION_FRONTEND_HEALTH_URL="${PRODUCTION_FRONTEND_HEALTH_URL:-http://192.168.10.17:8015/api/health}"

failures=0

section() {
  printf '\n== %s ==\n' "$1"
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  failures=$((failures + 1))
}

pass() {
  printf 'OK: %s\n' "$1"
}

section "GitHub runner"
if systemctl --user is-active --quiet "$RUNNER_SERVICE"; then
  pass "$RUNNER_SERVICE is active"
else
  fail "$RUNNER_SERVICE is not active"
  systemctl --user status "$RUNNER_SERVICE" --no-pager || true
fi

section "Staging Compose"
if [ ! -d "$STAGING_ROOT" ]; then
  fail "staging checkout is missing: $STAGING_ROOT"
else
  (
    cd "$STAGING_ROOT"
    docker compose ps
    running_services="$(docker compose ps --services --status running)"
    for service in person-counter mqtt-aggregator sen55-collector live-collector live-app vite-frontend; do
      if printf '%s\n' "$running_services" | grep -qx "$service"; then
        pass "staging service is running: $service"
      else
        fail "staging service is not running: $service"
      fi
    done
  )
fi

check_health() {
  local label="$1"
  local url="$2"
  local body

  section "$label"
  if ! body="$(curl --noproxy '*' -fsS "$url")"; then
    fail "$url is not reachable"
    return
  fi

  if HEALTH_JSON="$body" python3 - "$label" <<'PY'
import json
import os
import sys

label = sys.argv[1]
health = json.loads(os.environ["HEALTH_JSON"])
model_run_id = (health.get("inference") or {}).get("model_run_id")
ok = health.get("ok")
if not ok:
    raise SystemExit(f"{label}: health ok=false")
if not model_run_id:
    raise SystemExit(f"{label}: missing inference.model_run_id")
print(f"OK: {label} ok=true model_run_id={model_run_id}")
PY
  then
    :
  else
    fail "$url returned invalid health JSON"
  fi
}

check_health "Staging backend" "$STAGING_BACKEND_HEALTH_URL"
check_health "Staging frontend proxy" "$STAGING_FRONTEND_HEALTH_URL"
check_health "Production backend" "$PRODUCTION_BACKEND_HEALTH_URL"
check_health "Production frontend proxy" "$PRODUCTION_FRONTEND_HEALTH_URL"

section "Result"
if [ "$failures" -eq 0 ]; then
  pass "Zone 5 CI/CD health checks passed"
else
  fail "$failures check(s) failed"
  exit 1
fi
