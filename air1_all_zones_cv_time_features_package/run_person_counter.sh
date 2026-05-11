#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -d ".python-packages" ]]; then
  export PYTHONPATH="$PWD/.python-packages:$PWD${PYTHONPATH:+:$PYTHONPATH}"
fi
export YOLO_AUTOINSTALL="${YOLO_AUTOINSTALL:-False}"

CAMERA="${PERSON_COUNT_CAMERA_ID:-${CAMERA:-cam1}}"
if [[ "$CAMERA" != "cam1" && "$CAMERA" != "cam2" ]]; then
  echo "CAMERA must be cam1 or cam2." >&2
  exit 2
fi

SCRIPT="${SCRIPT:-rtsp_zone_tracker.py}"
MODEL="${MODEL:-cv_counter/models/headtracker-m.pt}"
TRACKER="${TRACKER:-cv_counter/trackers/botsort.yaml}"
DEVICE="${DEVICE:-cpu}"
ZONES="${PERSON_COUNT_ZONE_MAP:-${ZONES:-}}"
MASK="${PERSON_COUNT_MASK:-${MASK:-}}"
COUNTS_CSV="${COUNTS_CSV:-}"
MQTT_BROKER="${PERSON_COUNT_MQTT_BROKER:-${MQTT_BROKER:-10.158.71.19}}"
MQTT_PORT="${PERSON_COUNT_MQTT_PORT:-${MQTT_PORT:-1883}}"
MQTT_TOPIC="${PERSON_COUNT_MQTT_TOPIC:-${MQTT_TOPIC:-care_ssl/all_zones/person_count_by_zone}}"
MQTT_USERNAME="${PERSON_COUNT_MQTT_USERNAME:-${MQTT_USERNAME:-guest}}"
MQTT_PASSWORD="${PERSON_COUNT_MQTT_PASSWORD:-${MQTT_PASSWORD:-smartilab123}}"
MQTT_EVERY="${MQTT_EVERY:-1}"
COUNTS_EVERY="${COUNTS_EVERY:-1}"
LATEST_JPEG="${PERSON_COUNT_LATEST_JPEG:-${LATEST_JPEG:-}}"

if [[ -z "$ZONES" ]]; then
  if [[ "$CAMERA" == "cam1" ]]; then
    ZONES="cam1-zones.json"
  else
    ZONES="cam2-zones.json"
  fi
fi
if [[ -z "$MASK" ]]; then
  if [[ "$CAMERA" == "cam1" ]]; then
    MASK="masks/cam1-mask-zones.png"
  else
    MASK="masks/cam2-mask-zones.png"
  fi
fi
if [[ -z "$COUNTS_CSV" ]]; then
  COUNTS_CSV="data/person_counts_by_zone_${CAMERA}.csv"
fi
if [[ -z "$LATEST_JPEG" ]]; then
  if [[ "$CAMERA" == "cam1" ]]; then
    LATEST_JPEG="${AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM1:-data/yolo_latest_cam1.jpg}"
  else
    LATEST_JPEG="${AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM2:-data/yolo_latest_cam2.jpg}"
  fi
fi

SOURCE="${SOURCE:-}"
if [[ -z "$SOURCE" ]]; then
  if [[ "$CAMERA" == "cam1" ]]; then
    SOURCE="${AIR1_ALL_ZONES_RTSP_URL_CAM1:-rtsp://admin:++smartilab2023@10.158.71.241:554/Streaming/channels/101}"
  else
    SOURCE="${AIR1_ALL_ZONES_RTSP_URL_CAM2:-rtsp://admin:++smartilab2023@10.158.71.240:554/Streaming/channels/101}"
  fi
fi
if [[ -z "$SOURCE" ]]; then
  echo "Set SOURCE or AIR1_ALL_ZONES_RTSP_URL_${CAMERA^^} for $CAMERA." >&2
  exit 2
fi

require_file() {
  local label="$1"
  local path="$2"
  if [[ ! -f "$path" ]]; then
    echo "$label not found: $path. The AIR-1 all zones package must include tracker assets before starting the person counter." >&2
    exit 2
  fi
}

require_file "Zone tracker script" "$SCRIPT"
require_file "Zone map" "$ZONES"
require_file "Zone mask" "$MASK"
require_file "YOLO model file" "$MODEL"
require_file "Tracker config" "$TRACKER"

if ! "$PYTHON_BIN" -c "import lap" >/dev/null 2>&1; then
  echo "Python dependency 'lap' is missing. Run: python3 -m pip install --target .python-packages --upgrade -r requirements.txt" >&2
  exit 2
fi

mkdir -p "$(dirname "$COUNTS_CSV")"
mkdir -p "$(dirname "$LATEST_JPEG")"

exec "$PYTHON_BIN" "$SCRIPT" \
  --source "$SOURCE" \
  --model "$MODEL" \
  --tracker "$TRACKER" \
  --device "$DEVICE" \
  --zones "$ZONES" \
  --mask "$MASK" \
  --camera-id "$CAMERA" \
  --latest-jpeg "$LATEST_JPEG" \
  --counts-csv "$COUNTS_CSV" \
  --counts-every "$COUNTS_EVERY" \
  --mqtt-broker "$MQTT_BROKER" \
  --mqtt-port "$MQTT_PORT" \
  --mqtt-username "$MQTT_USERNAME" \
  --mqtt-password "$MQTT_PASSWORD" \
  --mqtt-topic "$MQTT_TOPIC" \
  --mqtt-every "$MQTT_EVERY"
