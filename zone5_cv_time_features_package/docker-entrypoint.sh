#!/usr/bin/env bash
set -euo pipefail

mkdir -p data model logs /tmp/ultralytics /tmp/matplotlib

exec "$@"
