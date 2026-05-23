#!/usr/bin/env bash
set -euo pipefail

mkdir -p \
  data \
  logs \
  stage \
  zone5_cv_time_features_package/data \
  zone5_cv_time_features_package/model \
  zone5_cv_time_features_package/logs \
  /tmp/matplotlib \
  /tmp/torchinductor

if [ ! -L smart_ilab.duckdb ] && [ ! -e smart_ilab.duckdb ]; then
  ln -s data/smart_ilab.duckdb smart_ilab.duckdb
fi

exec "$@"
