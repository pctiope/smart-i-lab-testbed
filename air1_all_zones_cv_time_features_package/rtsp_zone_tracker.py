#!/usr/bin/env python3
"""
RTSP / video multi-zone person tracker.

Zones are defined in a JSON config file.  Each zone has:
  - a name
  - a BGR color used to paint that zone on the mask image
  - an optional polygon (list of [x, y] points) to auto-paint onto the mask

The mask image is a color PNG where each zone's pixels carry that zone's
exact BGR color.  Black pixels (or anything not matching a zone color) are
ignored.

Filtering is done POST-DETECTION — YOLO runs on the full frame, then each
detection box is checked against every zone's binary mask.  A person is
counted in ALL zones whose mask overlaps their box (by area ratio or center).

──────────────────────────────────────────────────────────────────────────────
Zone JSON format  (zones.json)
──────────────────────────────────────────────────────────────────────────────
{
  "overlap_thresh": 0.3,
  "zones": [
    {
      "name": "Zone A – Left Tables",
      "color_bgr": [0, 0, 255],
      "polygon": [[120, 200], [520, 200], [520, 680], [120, 680]]
    },
    {
      "name": "Zone B – Right Tables",
      "color_bgr": [0, 255, 0],
      "polygon": [[560, 150], [1100, 150], [1100, 700], [560, 700]]
    },
    {
      "name": "Zone C – Back Area",
      "color_bgr": [255, 0, 0],
      "polygon": [[200, 50], [1000, 50], [1000, 140], [200, 140]]
    }
  ]
}

  overlap_thresh : float 0.0–1.0
      0.0  → center-point check only (fastest)
      0.3+ → fraction of bbox that must overlap the zone mask

  color_bgr : [B, G, R] — must exactly match the color painted on the mask image.
  polygon   : optional. If provided, these points are drawn onto the mask image
              automatically (useful when you don't want to paint manually).
              Polygon coords are in the original image space; they are scaled
              to the frame resolution at runtime.

──────────────────────────────────────────────────────────────────────────────
Usage
──────────────────────────────────────────────────────────────────────────────
# Mask image + JSON zones (recommended):
python rtsp_zone_tracker.py --source rtsp://... --model yolov8m.engine \\
    --zones zones.json --mask zone_mask.png

# JSON with polygons only (no mask image needed — mask is auto-generated):
python rtsp_zone_tracker.py --source rtsp://... --model yolov8m.engine \\
    --zones zones.json

# Save output + display:
python rtsp_zone_tracker.py --source video.mp4 --model yolov8m.engine \\
    --zones zones.json --mask zone_mask.png --display --show-zones --out out.mp4
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

MODEL_ZONE_IDS = set(range(1, 16))
EXCLUDED_ZONE_IDS = {16}
DEFAULT_ZONE_TOPIC = "care_ssl/all_zones/person_count_by_zone"
TABLE_NAME_PATTERN = re.compile(r"^\s*Table\s+(\d+)\s*$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Zone:
    name: str
    zone_id: int
    color_bgr: Tuple[int, int, int]   # color used in mask image
    polygon: Optional[np.ndarray]      # (N,1,2) int32, or None
    binary_mask: Optional[np.ndarray] = field(default=None, repr=False)
    # binary_mask is built at runtime from the color mask image

    @property
    def is_model_zone(self) -> bool:
        return self.zone_id in MODEL_ZONE_IDS


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Multi-zone person tracker — color-coded mask + JSON zone config."
    )
    p.add_argument("--source", "-s", required=True,
                   help="RTSP URL or video file path")
    p.add_argument("--model", default="yolov8n.pt",
                   help="YOLO model path (e.g. yolov8m.engine)")
    p.add_argument("--zones", required=True,
                   help="Path to zones JSON config file")
    p.add_argument("--mask",
                   help="Color-coded mask image (PNG). If omitted, auto-generated from polygon coords in JSON.")
    p.add_argument("--tracker", default="botsort.yaml",
                   help="botsort.yaml or bytetrack.yaml (default: botsort.yaml)")
    p.add_argument("--device", default="0",
                   help="0 = first GPU, cpu = CPU (default: 0)")
    p.add_argument("--conf", type=float, default=0.3,
                   help="Detection confidence threshold (default: 0.3)")
    p.add_argument("--iou", type=float, default=0.3,
                   help="NMS IoU threshold (default: 0.3)")
    p.add_argument("--imgsz", type=int, default=640,
                   help="Inference image size (default: 640)")
    p.add_argument("--out",
                   help="Output video file path")
    p.add_argument("--latest-jpeg",
                   help="Write the latest annotated frame to this JPEG path for web MJPEG streaming.")
    p.add_argument("--display", action="store_true",
                   help="Show live window")
    p.add_argument("--show-zones", action="store_true",
                   help="Draw zone overlays and boundaries on output")
    p.add_argument("--save-mask", metavar="PATH",
                   help="Save the generated/loaded color mask to this path and exit (useful for editing)")
    p.add_argument("--max-persons", type=int, default=100,)
    p.add_argument("--camera-id", default=os.getenv("PERSON_COUNT_CAMERA_ID"),
                   help="Camera identifier to include in MQTT/CSV payloads, for example cam1 or cam2.")
    p.add_argument("--counts-csv", default="data/person_counts_by_zone.csv",
                   help="Per-frame per-zone count CSV path. Default: data/person_counts_by_zone.csv")
    p.add_argument("--counts-every", type=int, default=1,
                   help="Write one count row every N processed frames. Default: 1")
    p.add_argument("--mqtt-broker", default=os.getenv("PERSON_COUNT_MQTT_BROKER"),
                   help="MQTT broker host. Omit to disable MQTT publishing.")
    p.add_argument("--mqtt-port", type=int, default=int(os.getenv("PERSON_COUNT_MQTT_PORT", "1883")),
                   help="MQTT broker port. Default: 1883")
    p.add_argument("--mqtt-topic", default=os.getenv("PERSON_COUNT_MQTT_TOPIC", DEFAULT_ZONE_TOPIC),
                   help=f"MQTT topic for per-zone count JSON. Default: {DEFAULT_ZONE_TOPIC}")
    p.add_argument("--mqtt-username", default=os.getenv("PERSON_COUNT_MQTT_USERNAME"),
                   help="Optional MQTT username.")
    p.add_argument("--mqtt-password", default=os.getenv("PERSON_COUNT_MQTT_PASSWORD"),
                   help="Optional MQTT password.")
    p.add_argument("--mqtt-client-id", default=os.getenv("PERSON_COUNT_MQTT_CLIENT_ID", "care_ssl_zone_tracker"),
                   help="MQTT client ID. Default: care_ssl_zone_tracker")
    p.add_argument("--mqtt-qos", type=int, choices=[0, 1, 2], default=int(os.getenv("PERSON_COUNT_MQTT_QOS", "0")),
                   help="MQTT publish QoS. Default: 0")
    p.add_argument("--mqtt-retain", action="store_true",
                   help="Publish retained MQTT messages.")
    p.add_argument("--mqtt-every", type=int, default=1,
                   help="Publish one MQTT message every N processed frames. Default: 1")
    return p.parse_args()


def write_latest_jpeg(frame: np.ndarray, path: str | None, quality: int = 70) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    success, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not success:
        raise RuntimeError(f"Could not encode latest annotated frame as JPEG: {target}")
    tmp_path = target.with_name(f".{target.name}.tmp")
    tmp_path.write_bytes(buffer.tobytes())
    tmp_path.replace(target)


# ─────────────────────────────────────────────────────────────────────────────
# Zone loading
# ─────────────────────────────────────────────────────────────────────────────

def table_name_to_zone_id(name: str) -> int:
    match = TABLE_NAME_PATTERN.match(str(name))
    if not match:
        raise ValueError(f"Zone name must use 'Table N' so it can map to AIR-1 zone_id: {name!r}")
    zone_id = int(match.group(1))
    if zone_id not in MODEL_ZONE_IDS and zone_id not in EXCLUDED_ZONE_IDS:
        allowed = "1-15 plus Table 16 as an excluded/unlabeled zone"
        raise ValueError(f"Unsupported table mapping {name!r}; expected {allowed}.")
    return zone_id


def load_zones(json_path: str) -> Tuple[List[Zone], float]:
    """Parse zones.json → list of Zone objects + global overlap_thresh."""
    with open(json_path) as f:
        cfg = json.load(f)

    overlap_thresh = float(cfg.get("overlap_thresh", 0.0))
    zones = []
    for z in cfg["zones"]:
        zone_id = table_name_to_zone_id(z["name"])
        b, g, r = z["color_bgr"]
        poly = None
        if "polygon" in z and z["polygon"]:
            pts = np.array(z["polygon"], dtype=np.float32)   # (N, 2) float
            poly = pts  # store as float; convert to int32 after we know frame size
        zones.append(Zone(
            name=z["name"],
            zone_id=zone_id,
            color_bgr=(int(b), int(g), int(r)),
            polygon=poly,
        ))
    return zones, overlap_thresh


def build_zone_masks(
    zones: List[Zone],
    color_mask_img: Optional[np.ndarray],
    frame_hw: Tuple[int, int],
    orig_hw: Optional[Tuple[int, int]] = None,
) -> None:
    """
    For each zone, build a binary uint8 mask (H, W) using one of two sources:
      1. color_mask_img: pixels matching zone.color_bgr → 255
      2. zone.polygon: drawn as filled poly on a blank canvas (scaled to frame_hw)
    Sets zone.binary_mask in-place.
    """
    H, W = frame_hw
    color_tol = 15  # allow slight color deviation for JPEG/PNG compression

    for zone in zones:
        b, g, r = zone.color_bgr

        if color_mask_img is not None:
            # ── Source 1: color mask image ────────────────────────────────────
            img = color_mask_img
            if img.shape[:2] != (H, W):
                img = cv2.resize(img, (W, H), interpolation=cv2.INTER_NEAREST)

            lower = np.array([max(0, b - color_tol),
                               max(0, g - color_tol),
                               max(0, r - color_tol)], dtype=np.uint8)
            upper = np.array([min(255, b + color_tol),
                               min(255, g + color_tol),
                               min(255, r + color_tol)], dtype=np.uint8)
            zone.binary_mask = cv2.inRange(img, lower, upper)

        elif zone.polygon is not None:
            # ── Source 2: polygon from JSON ───────────────────────────────────
            canvas = np.zeros((H, W), dtype=np.uint8)
            pts = zone.polygon.copy()
            # scale if original resolution is known and differs
            if orig_hw is not None and orig_hw != (H, W):
                oh, ow = orig_hw
                pts[:, 0] *= W / ow
                pts[:, 1] *= H / oh
            pts_i = pts.astype(np.int32).reshape((-1, 1, 2))
            cv2.fillPoly(canvas, [pts_i], 255)
            zone.binary_mask = canvas

        else:
            # Zone has neither mask pixels nor polygon — treat as full frame
            zone.binary_mask = np.full((H, W), 255, dtype=np.uint8)

    print("Zone binary masks built:")
    for z in zones:
        n = int(np.count_nonzero(z.binary_mask))
        pct = 100.0 * n / (H * W)
        print(f"  [{z.color_bgr}] {z.name}: {n} px ({pct:.1f}%)")


def generate_color_mask(
    zones: List[Zone],
    frame_hw: Tuple[int, int],
    orig_hw: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """Build a BGR color mask image from polygon-defined zones (for saving/editing)."""
    H, W = frame_hw
    canvas = np.zeros((H, W, 3), dtype=np.uint8)
    for zone in zones:
        if zone.polygon is None:
            continue
        pts = zone.polygon.copy()
        if orig_hw is not None and orig_hw != (H, W):
            oh, ow = orig_hw
            pts[:, 0] *= W / ow
            pts[:, 1] *= H / oh
        pts_i = pts.astype(np.int32).reshape((-1, 1, 2))
        cv2.fillPoly(canvas, [pts_i], zone.color_bgr)
    return canvas


# ─────────────────────────────────────────────────────────────────────────────
# Per-detection zone assignment
# ─────────────────────────────────────────────────────────────────────────────

def get_zones_for_box(
    zones: List[Zone],
    x1: int, y1: int, x2: int, y2: int,
    overlap_thresh: float,
) -> List[Zone]:
    """
    Return all zones that this bounding box belongs to.
    overlap_thresh == 0.0 → center-point check (O(1) per zone).
    overlap_thresh  > 0.0 → area overlap ratio check (slightly heavier but still cheap).
    """
    matched = []
    H, W = zones[0].binary_mask.shape[:2]

    # Clamp box to frame
    bx1 = max(0, x1); by1 = max(0, y1)
    bx2 = min(W, x2); by2 = min(H, y2)

    if overlap_thresh <= 0.0:
        # Center-point lookup — O(1)
        cx = (bx1 + bx2) // 2
        cy = (by1 + by2) // 2
        cy = min(cy, H - 1); cx = min(cx, W - 1)
        for zone in zones:
            if zone.binary_mask[cy, cx] > 0:
                matched.append(zone)
    else:
        # Area overlap ratio
        box_area = max(1, (bx2 - bx1) * (by2 - by1))
        for zone in zones:
            roi = zone.binary_mask[by1:by2, bx1:bx2]
            if roi.size == 0:
                continue
            ratio = float(np.count_nonzero(roi)) / box_area
            if ratio >= overlap_thresh:
                matched.append(zone)

    return matched


def get_zone_overlap_ratio(
    zone: Zone,
    x1: int, y1: int, x2: int, y2: int,
) -> float:
    if zone.binary_mask is None:
        return 0.0
    H, W = zone.binary_mask.shape[:2]
    bx1 = max(0, x1); by1 = max(0, y1)
    bx2 = min(W, x2); by2 = min(H, y2)
    if bx2 <= bx1 or by2 <= by1:
        return 0.0
    box_area = max(1, (bx2 - bx1) * (by2 - by1))
    roi = zone.binary_mask[by1:by2, bx1:bx2]
    if roi.size == 0:
        return 0.0
    return float(np.count_nonzero(roi)) / box_area


def assign_zone_for_box(
    zones: List[Zone],
    x1: int, y1: int, x2: int, y2: int,
    overlap_thresh: float,
) -> Optional[Zone]:
    """Assign a detection to the single model zone with the largest mask overlap."""
    best_zone: Optional[Zone] = None
    best_ratio = 0.0
    for zone in zones:
        if not zone.is_model_zone:
            continue
        ratio = get_zone_overlap_ratio(zone, x1, y1, x2, y2)
        if ratio > best_ratio:
            best_ratio = ratio
            best_zone = zone
    min_ratio = max(0.0, float(overlap_thresh))
    if best_zone is None or best_ratio <= 0.0 or best_ratio < min_ratio:
        return None
    return best_zone


# ─────────────────────────────────────────────────────────────────────────────
# Visualization helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_zone_overlay(zones: List[Zone], alpha: float = 0.25) -> np.ndarray:
    """
    Build a transparent colored overlay showing all zones.
    Returns a BGR uint8 image (same H×W as masks) to be blended with each frame.
    """
    H, W = zones[0].binary_mask.shape[:2]
    overlay = np.zeros((H, W, 3), dtype=np.uint8)
    for zone in zones:
        colored = np.zeros((H, W, 3), dtype=np.uint8)
        colored[zone.binary_mask > 0] = zone.color_bgr
        overlay = cv2.add(overlay, colored)
    return overlay   # caller blends: cv2.addWeighted(frame, 1-a, overlay, a, 0)


def draw_zone_boundaries(frame: np.ndarray, zones: List[Zone], thickness: int = 2) -> None:
    """Draw each zone's contour on the frame in its own color, with name label."""
    for zone in zones:
        contours, _ = cv2.findContours(
            zone.binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(frame, contours, -1, zone.color_bgr, thickness)
        if contours:
            # Put zone name near the topmost point of the contour
            top = tuple(contours[0][contours[0][:, :, 1].argmin()][0])
            cv2.putText(frame, zone.name, (top[0], max(15, top[1] - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, zone.color_bgr, 2)


def draw_hud(frame: np.ndarray, zone_counts: dict, fps: float, y_start: int = 30) -> None:
    """Draw per-zone person counts + FPS as a HUD in the top-left corner."""
    lines = [f"FPS: {fps:.1f}"] + [
        f"{name}: {count} person{'s' if count != 1 else ''}"
        for name, count in zone_counts.items()
    ] + [
        f"TOTAL: {sum(zone_counts.values())} person{'s' if sum(zone_counts.values()) != 1 else ''}"
    ]
    pad = 8
    line_h = 24
    max_w = max(
        cv2.getTextSize(l, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)[0][0] for l in lines
    )
    box_h = line_h * len(lines) + pad * 2
    # semi-transparent background
    roi = frame[y_start - pad: y_start + box_h, 10: 10 + max_w + pad * 2]
    if roi.size > 0:
        dark = (roi * 0.45).astype(np.uint8)
        frame[y_start - pad: y_start + box_h, 10: 10 + max_w + pad * 2] = dark

    for i, line in enumerate(lines):
        color = (0, 255, 255) if i == 0 else (255, 255, 255)
        cv2.putText(frame, line,
                    (10 + pad, y_start + i * line_h + line_h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Misc
# ─────────────────────────────────────────────────────────────────────────────

def resolve_device(value: str):
    s = value.strip()
    return int(s) if s.isdigit() else s


def infer_camera_id(args: argparse.Namespace) -> str:
    if args.camera_id:
        return str(args.camera_id)
    stem = Path(args.zones).stem.lower()
    if stem.startswith("cam1"):
        return "cam1"
    if stem.startswith("cam2"):
        return "cam2"
    return stem or "camera"


def load_yolo_model(model_path: str):
    try:
        from ultralytics import YOLO
    except ImportError as e:
        print("Please install ultralytics: pip install ultralytics. Error:", e)
        sys.exit(1)
    return YOLO(model_path)


def create_mqtt_client(client_id: str):
    try:
        from paho.mqtt import client as mqtt_client
    except ImportError as e:
        print("MQTT publishing requires paho-mqtt. Install it with: pip install paho-mqtt")
        sys.exit(1)

    try:
        return mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1, client_id)
    except AttributeError:
        return mqtt_client.Client(client_id)
    except TypeError:
        return mqtt_client.Client(client_id)


def connect_mqtt(args):
    if not args.mqtt_broker:
        return None

    client = create_mqtt_client(args.mqtt_client_id)
    if args.mqtt_username or args.mqtt_password:
        client.username_pw_set(args.mqtt_username, args.mqtt_password)

    def on_connect(mqtt, userdata, flags, rc):
        if rc == 0:
            print(f"Connected to MQTT broker {args.mqtt_broker}:{args.mqtt_port}")
            print(f"Publishing per-zone counts to topic: {args.mqtt_topic}")
        else:
            print(f"MQTT connection failed with rc={rc}")

    def on_disconnect(mqtt, userdata, rc):
        if rc != 0:
            print(f"MQTT disconnected unexpectedly with rc={rc}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    try:
        client.connect(args.mqtt_broker, args.mqtt_port, keepalive=60)
    except OSError as e:
        print(f"Could not connect to MQTT broker {args.mqtt_broker}:{args.mqtt_port}: {e}")
        sys.exit(1)
    client.loop_start()
    return client


def publish_count_payload(mqtt_client, args, payload: dict) -> None:
    if mqtt_client is None:
        return
    message = json.dumps(payload, separators=(",", ":"))
    info = mqtt_client.publish(args.mqtt_topic, message, qos=args.mqtt_qos, retain=args.mqtt_retain)
    if info.rc != 0:
        print(f"MQTT publish failed with rc={info.rc}")


def count_csv_headers() -> list[str]:
    return [
        "timestamp",
        "elapsed_seconds",
        "frame_index",
        "camera_id",
        "source_fps",
        "processing_fps",
        "raw_person_detections",
        "counted_persons",
        "counts_by_zone",
        "unlabeled_zones",
        "zone_map",
        "mask",
        "label_scope",
    ]


def write_count_csv_row(writer: csv.writer, payload: dict) -> None:
    writer.writerow([
        payload["timestamp"],
        payload["elapsed_seconds"],
        payload["frame_index"],
        payload["camera_id"],
        payload["source_fps"],
        payload["processing_fps"],
        payload["raw_person_detections"],
        payload["counted_persons"],
        json.dumps(payload["counts_by_zone"], sort_keys=True, separators=(",", ":")),
        json.dumps(payload["unlabeled_zones"], separators=(",", ":")),
        payload["zone_map"],
        payload["mask"],
        payload["label_scope"],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    device = resolve_device(args.device)
    camera_id = infer_camera_id(args)
    counts_every = max(1, int(args.counts_every))
    mqtt_every = max(1, int(args.mqtt_every))

    # ── Load zones config ─────────────────────────────────────────────────────
    zones, overlap_thresh = load_zones(args.zones)
    print(f"Loaded {len(zones)} zone(s) from {args.zones}")
    for z in zones:
        print(f"  • {z.name}  color={z.color_bgr}  polygon={'yes' if z.polygon is not None else 'no'}")

    # ── Load color mask image (if provided) ───────────────────────────────────
    color_mask_img = None
    if args.mask:
        color_mask_img = cv2.imread(args.mask)
        if color_mask_img is None:
            print(f"ERROR: Cannot load mask image: {args.mask}")
            sys.exit(1)
        print(f"Color mask image loaded: {args.mask}  shape={color_mask_img.shape}")

    # ── Load YOLO model ───────────────────────────────────────────────────────
    print(f"Loading model: {args.model}")
    model = load_yolo_model(args.model)

    # ── Open video source ─────────────────────────────────────────────────────
    cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        print(f"ERROR: Cannot open source: {args.source}")
        sys.exit(1)

    fps_src = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height  = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Source: {width}×{height} @ {fps_src:.1f} fps")

    # ── Build per-zone binary masks ───────────────────────────────────────────
    # If polygon coords were provided without a mask image, we generate the mask
    orig_hw = color_mask_img.shape[:2] if color_mask_img is not None else None
    build_zone_masks(zones, color_mask_img, frame_hw=(height, width), orig_hw=orig_hw)

    # ── Optionally save the generated color mask then exit ────────────────────
    if args.save_mask:
        gen = generate_color_mask(zones, frame_hw=(height, width))
        cv2.imwrite(args.save_mask, gen)
        print(f"Color mask saved to {args.save_mask}. Open it in GIMP/Photoshop to edit zones.")
        cap.release()
        return

    # ── Pre-compute static zone overlay (blended once per frame cheaply) ──────
    zone_color_overlay = build_zone_overlay(zones, alpha=0.25) if args.show_zones else None

    # ── Output writer ─────────────────────────────────────────────────────────
    writer = None
    if args.out:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(args.out, fourcc, max(1.0, fps_src), (width, height))
        print(f"Writing output → {args.out}")

    # ── Rolling FPS ───────────────────────────────────────────────────────────
    counts_file = None
    counts_writer = None
    if args.counts_csv:
        counts_path = Path(args.counts_csv)
        counts_path.parent.mkdir(parents=True, exist_ok=True)
        counts_file = counts_path.open("w", newline="", encoding="utf-8")
        counts_writer = csv.writer(counts_file)
        counts_writer.writerow(count_csv_headers())
        counts_file.flush()
        print(f"Writing per-zone count CSV to: {counts_path}")

    mqtt_client = connect_mqtt(args)

    fps_window = deque(maxlen=30)
    last_time  = time.perf_counter()
    start_wall_time = time.time()

    print(f"\nTracker      : {args.tracker}")
    print(f"Device       : {device}")
    print(f"Conf / IoU   : {args.conf} / {args.iou}")
    print(f"Latest JPEG  : {args.latest_jpeg or 'disabled'}")
    print(f"Overlap mode : {'ratio≥' + str(overlap_thresh) if overlap_thresh > 0 else 'center-point'}")
    print("Running — press Ctrl+C or 'q' to stop.\n")

    frame_idx = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Stream ended or frame read failed.")
                break
            frame_idx += 1

            # ── YOLO track on full frame ──────────────────────────────────────
            results = model.track(
                source=frame,
                conf=args.conf,
                iou=args.iou,
                classes=[0],
                device=device,
                imgsz=args.imgsz,
                tracker=args.tracker,
                persist=True,
                verbose=False,
                max_det=args.max_persons,
            )

            # ── Prepare visualization frame ───────────────────────────────────
            vis = frame.copy()

            # Blend zone color overlay
            if zone_color_overlay is not None:
                cv2.addWeighted(vis, 0.75, zone_color_overlay, 0.25, 0, vis)

            # Draw zone boundaries + labels
            if args.show_zones:
                draw_zone_boundaries(vis, zones)

            # ── Per-zone counters (reset each frame) ──────────────────────────
            zone_counts = {z.zone_id: 0 for z in zones if z.is_model_zone}
            raw_person_detections = 0

            r = results[0]
            if r.boxes is not None:
                boxes_xyxy = r.boxes.xyxy.cpu().numpy().astype(int)
                raw_person_detections = len(boxes_xyxy)
                if r.boxes.id is not None:
                    track_ids = r.boxes.id.cpu().numpy().astype(int).tolist()
                else:
                    track_ids = [None] * raw_person_detections
                confs = r.boxes.conf.cpu().numpy() if r.boxes.conf is not None else np.ones(raw_person_detections)

                for (x1, y1, x2, y2), tid, conf in zip(boxes_xyxy, track_ids, confs):

                    # ── Find which zones this box belongs to ──────────────────
                    matched_zone = assign_zone_for_box(
                        zones, x1, y1, x2, y2, overlap_thresh)

                    if matched_zone is None:
                        continue  # outside all zones — skip entirely

                    zone_counts[matched_zone.zone_id] += 1

                    box_color = matched_zone.color_bgr

                    # Draw bounding box
                    cv2.rectangle(vis, (x1, y1), (x2, y2), box_color, 2)

                    # Label: ID + zone + conf
                    label_id = f"ID{tid}" if tid is not None else "person"
                    label = f"{label_id} [Z{matched_zone.zone_id}] {conf:.2f}"
                    label_y = max(20, y1 - 6)
                    (lw, lh), bl = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
                    cv2.rectangle(vis,
                                  (x1, label_y - lh - bl),
                                  (x1 + lw, label_y + bl),
                                  box_color, cv2.FILLED)
                    cv2.putText(vis, label, (x1, label_y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 2)

            # ── HUD ───────────────────────────────────────────────────────────
            now = time.perf_counter()
            fps_window.append(1.0 / max(1e-6, now - last_time))
            last_time = now
            avg_fps = sum(fps_window) / len(fps_window)

            draw_hud(vis, zone_counts, avg_fps)
            write_latest_jpeg(vis, args.latest_jpeg)

            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elapsed_seconds = round(time.time() - start_wall_time, 3)
            source_fps_value = round(float(fps_src), 3)
            processing_fps_value = round(float(avg_fps), 3)
            counts_by_zone = {str(zone_id): int(count) for zone_id, count in sorted(zone_counts.items())}
            unlabeled_zones = sorted(z.zone_id for z in zones if z.zone_id in EXCLUDED_ZONE_IDS)
            count_payload = {
                "timestamp": timestamp_str,
                "camera_id": camera_id,
                "frame_index": frame_idx,
                "elapsed_seconds": elapsed_seconds,
                "source": args.source,
                "source_fps": source_fps_value,
                "processing_fps": processing_fps_value,
                "raw_person_detections": raw_person_detections,
                "counted_persons": int(sum(zone_counts.values())),
                "counts_by_zone": counts_by_zone,
                "unlabeled_zones": unlabeled_zones,
                "assignment_rule": "largest_overlap",
                "label_scope": "per_zone",
                "zone_map": args.zones,
                "mask": args.mask or "",
                "model": args.model,
            }

            if counts_writer is not None and frame_idx % counts_every == 0:
                write_count_csv_row(counts_writer, count_payload)
                counts_file.flush()

            if mqtt_client is not None and frame_idx % mqtt_every == 0:
                publish_count_payload(mqtt_client, args, count_payload)

            # ── Display / write ───────────────────────────────────────────────
            if args.display:
                cv2.imshow("Zone Tracker", vis)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("'q' pressed.")
                    break

            if writer:
                writer.write(vis)

            if frame_idx % 100 == 0:
                counts_str = "  ".join(f"{k}: {v}" for k, v in zone_counts.items())
                print(f"  Frame {frame_idx:6d} | FPS {avg_fps:5.1f} | {counts_str}")

    except KeyboardInterrupt:
        print("\nInterrupted.")

    finally:
        cap.release()
        if writer:
            writer.release()
        if counts_file:
            counts_file.close()
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        cv2.destroyAllWindows()
        print(f"\nDone. Processed {frame_idx} frames.")


if __name__ == "__main__":
    main()
