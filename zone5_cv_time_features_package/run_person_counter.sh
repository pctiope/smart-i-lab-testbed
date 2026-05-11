#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -d ".python-packages" ]]; then
  export PYTHONPATH="$PWD/.python-packages:$PWD${PYTHONPATH:+:$PYTHONPATH}"
fi
export YOLO_AUTOINSTALL="${YOLO_AUTOINSTALL:-False}"

SOURCE="${ZONE5_RTSP_URL:-${SOURCE:-rtsp://admin:++smartilab2023@10.158.71.241:554/Streaming/channels/102}}"
SCRIPT="${SCRIPT:-cv_counter/rtsp_person_mask_tracker_new.py}"
MASK="${MASK:-cv_counter/masks/cam1-desk5-mask.png}"
MODEL="${MODEL:-cv_counter/models/headtracker-m.pt}"
TRACKER="${TRACKER:-cv_counter/trackers/bytetrack.yaml}"
DEVICE="${DEVICE:-cpu}"
IMGSZ="${PERSON_COUNT_IMGSZ:-${IMGSZ:-256}}"
CONF="${PERSON_COUNT_CONF:-${CONF:-0.4}}"
IOU="${PERSON_COUNT_IOU:-${IOU:-0.4}}"
TRACKING="${PERSON_COUNT_TRACKING:-${TRACKING:-1}}"
ROI_FROM_MASK="${PERSON_COUNT_ROI_FROM_MASK:-${ROI_FROM_MASK:-1}}"
ROI_MARGIN="${PERSON_COUNT_ROI_MARGIN:-${ROI_MARGIN:-32}}"
SHOW_MASK="${PERSON_COUNT_SHOW_MASK:-${SHOW_MASK:-1}}"
COUNTS_CSV="${COUNTS_CSV:-data/person_counts.csv}"
MQTT_BROKER="${PERSON_COUNT_MQTT_BROKER:-${MQTT_BROKER:-10.158.71.19}}"
MQTT_PORT="${PERSON_COUNT_MQTT_PORT:-${MQTT_PORT:-1883}}"
MQTT_TOPIC="${PERSON_COUNT_MQTT_TOPIC:-${MQTT_TOPIC:-care_ssl/zone5/person_count}}"
MQTT_USERNAME="${PERSON_COUNT_MQTT_USERNAME:-${MQTT_USERNAME:-guest}}"
MQTT_PASSWORD="${PERSON_COUNT_MQTT_PASSWORD:-${MQTT_PASSWORD:-smartilab123}}"
MQTT_EVERY="${PERSON_COUNT_MQTT_EVERY:-${MQTT_EVERY:-5}}"
COUNTS_EVERY="${PERSON_COUNT_COUNTS_EVERY:-${COUNTS_EVERY:-5}}"
LATEST_JPEG="${PERSON_COUNT_LATEST_JPEG:-${LATEST_JPEG:-${ZONE5_ANNOTATED_FRAME_PATH:-data/yolo_latest.jpg}}}"

if [[ -z "$SOURCE" ]]; then
  echo "Set ZONE5_RTSP_URL or SOURCE to the camera RTSP URL." >&2
  exit 2
fi

require_file() {
  local label="$1"
  local path="$2"
  if [[ ! -f "$path" ]]; then
    echo "$label not found: $path. The Zone 5 package must include cv_counter assets before starting the person counter." >&2
    exit 2
  fi
}

require_file "Person-counter script" "$SCRIPT"
require_file "Mask file" "$MASK"
require_file "YOLO model file" "$MODEL"
require_file "Tracker config" "$TRACKER"

if ! "$PYTHON_BIN" -c "import lap" >/dev/null 2>&1; then
  echo "Python dependency 'lap' is missing. Run: python3 -m pip install --target .python-packages --upgrade -r requirements.txt" >&2
  exit 2
fi

mkdir -p "$(dirname "$COUNTS_CSV")"
mkdir -p "$(dirname "$LATEST_JPEG")"

ROI_ARGS=()
case "${ROI_FROM_MASK,,}" in
  1|true|yes|on)
    ROI_ARGS=(--roi-from-mask --roi-margin "$ROI_MARGIN")
    ;;
  0|false|no|off|disabled)
    ;;
  *)
    echo "PERSON_COUNT_ROI_FROM_MASK/ROI_FROM_MASK must be true or false, got: $ROI_FROM_MASK" >&2
    exit 2
    ;;
esac

MASK_VIS_ARGS=()
case "${SHOW_MASK,,}" in
  1|true|yes|on)
    MASK_VIS_ARGS=(--show-mask)
    ;;
  0|false|no|off|disabled)
    ;;
  *)
    echo "PERSON_COUNT_SHOW_MASK/SHOW_MASK must be true or false, got: $SHOW_MASK" >&2
    exit 2
    ;;
esac

TRACKING_ARGS=()
case "${TRACKING,,}" in
  1|true|yes|on)
    ;;
  0|false|no|off|disabled)
    TRACKING_ARGS=(--no-tracker)
    ;;
  *)
    echo "PERSON_COUNT_TRACKING/TRACKING must be true or false, got: $TRACKING" >&2
    exit 2
    ;;
esac

exec "$PYTHON_BIN" "$SCRIPT" \
  --source "$SOURCE" \
  --mask "$MASK" \
  --model "$MODEL" \
  --tracker "$TRACKER" \
  --device "$DEVICE" \
  --imgsz "$IMGSZ" \
  --conf "$CONF" \
  --iou "$IOU" \
  "${TRACKING_ARGS[@]}" \
  "${ROI_ARGS[@]}" \
  "${MASK_VIS_ARGS[@]}" \
  --latest-jpeg "$LATEST_JPEG" \
  --counts-csv "$COUNTS_CSV" \
  --counts-every "$COUNTS_EVERY" \
  --mqtt-broker "$MQTT_BROKER" \
  --mqtt-port "$MQTT_PORT" \
  --mqtt-username "$MQTT_USERNAME" \
  --mqtt-password "$MQTT_PASSWORD" \
  --mqtt-topic "$MQTT_TOPIC" \
  --mqtt-every "$MQTT_EVERY"
