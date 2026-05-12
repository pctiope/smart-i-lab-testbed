#!/usr/bin/env python3
"""
RTSP real-time person tracking with YOLO (detection) + BotSort/ByteTrack (tracker).
Only detections inside a provided mask are considered — via post-detection filtering
(zero pixel-manipulation cost).

Fixes over old version:
- Mask applied POST-detection (filter by box center or overlap ratio), not pre-detection
- model.track() called with persist=True so tracker state is maintained across frames
- source= correctly passes the frame variable, not a string literal
- Removed redundant SimpleIoUTracker — YOLO's built-in tracker IDs are used directly
- Removed batch=32 on single-frame inference
- Mask visualization dims the live frame, so background outside the mask keeps updating
- model.to(device) replaced with passing device= directly in track() call
- FPS counter uses a rolling window for more accurate real-time reporting

Usage:
    python rtsp_person_mask_tracker.py --source rtsp://... --mask mask.png --model yolov8m.engine
    python rtsp_person_mask_tracker.py --source video.mp4 --mask mask.png --model yolov8m.engine --display
"""

import argparse
import csv
import json
import os
import time
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

os.environ.setdefault("YOLO_AUTOINSTALL", "False")


# ─────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="RTSP/video person tracking within a mask — post-detection filtering, no pixel masking."
    )
    p.add_argument("--source", "-s", required=True,
                   help="RTSP URL or video file path")
    p.add_argument("--model", default="yolov8n.pt",
                   help="YOLO model path (e.g. yolov8m.engine for TensorRT)")
    p.add_argument("--mask", default="masks/cam1-desk5-mask.png",
                   help="Binary mask image path (white = active region). Default: masks/cam1-desk5-mask.png")
    p.add_argument("--tracker", default="botsort.yaml",
                   help="Tracker config: botsort.yaml or bytetrack.yaml (default: botsort.yaml)")
    p.add_argument("--no-tracker", action="store_true",
                   help="Use detector-only inference instead of BoT-SORT tracking. Faster, but track IDs are omitted.")
    p.add_argument("--device", default="0",
                   help="Inference device: 0 for GPU, cpu for CPU (default: 0)")
    p.add_argument("--conf", type=float, default=0.3,
                   help="Detection confidence threshold (default: 0.3)")
    p.add_argument("--iou", type=float, default=0.3,
                   help="NMS IoU threshold (default: 0.3)")
    p.add_argument("--imgsz", type=int, default=640,
                   help="Inference image size (default: 640)")
    p.add_argument("--mask-overlap", type=float, default=0.0,
                   help=(
                       "Min fraction of the bounding box that must overlap the mask to count. "
                       "0.0 = center-point check only (fastest). "
                       "0.3–0.5 = stricter overlap check (slightly slower). "
                       "(default: 0.0)"
                   ))
    p.add_argument("--roi-from-mask", action="store_true",
                   help="Run YOLO only on the mask bounding box, then map detections back to the full frame.")
    p.add_argument("--roi-margin", type=int, default=32,
                   help="Pixel margin around the mask bounding box when --roi-from-mask is enabled. Default: 32")
    p.add_argument("--out",
                   help="Optional output video path to save annotated frames")
    p.add_argument("--latest-jpeg",
                   help="Write the latest annotated frame to this JPEG path for web MJPEG streaming.")
    p.add_argument("--counts-csv", default="person_counts.csv",
                   help="Per-frame person-count CSV path. Default: person_counts.csv")
    p.add_argument("--counts-every", type=int, default=1,
                   help="Write one count row every N processed frames. Default: 1")
    p.add_argument("--mqtt-broker", default=os.getenv("PERSON_COUNT_MQTT_BROKER"),
                   help="MQTT broker host. Omit to disable MQTT publishing.")
    p.add_argument("--mqtt-port", type=int, default=int(os.getenv("PERSON_COUNT_MQTT_PORT", "1883")),
                   help="MQTT broker port. Default: 1883")
    p.add_argument("--mqtt-topic", default=os.getenv("PERSON_COUNT_MQTT_TOPIC", "care_ssl/zone5/person_count"),
                   help="MQTT topic for count JSON. Default: care_ssl/zone5/person_count")
    p.add_argument("--mqtt-username", default=os.getenv("PERSON_COUNT_MQTT_USERNAME"),
                   help="Optional MQTT username.")
    p.add_argument("--mqtt-password", default=os.getenv("PERSON_COUNT_MQTT_PASSWORD"),
                   help="Optional MQTT password.")
    p.add_argument("--mqtt-client-id", default=os.getenv("PERSON_COUNT_MQTT_CLIENT_ID", "care_ssl_person_counter"),
                   help="MQTT client ID. Default: care_ssl_person_counter")
    p.add_argument("--mqtt-qos", type=int, choices=[0, 1, 2], default=int(os.getenv("PERSON_COUNT_MQTT_QOS", "0")),
                   help="MQTT publish QoS. Default: 0")
    p.add_argument("--mqtt-retain", action="store_true",
                   help="Publish retained MQTT messages.")
    p.add_argument("--mqtt-every", type=int, default=1,
                   help="Publish one MQTT message every N processed frames. Default: 1")
    p.add_argument("--read-failures-before-reconnect", type=int, default=5,
                   help="Consecutive failed live-stream reads before reconnecting. Default: 5")
    p.add_argument("--reconnect-delay", type=float, default=2.0,
                   help="Initial live-stream reconnect delay in seconds. Default: 2.0")
    p.add_argument("--max-reconnect-delay", type=float, default=30.0,
                   help="Maximum live-stream reconnect delay in seconds. Default: 30.0")
    p.add_argument("--display", action="store_true",
                   help="Show live OpenCV window")
    p.add_argument("--show-mask", action="store_true",
                   help="Dim regions outside the mask in the display/output")
    return p.parse_args()


# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────

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


def load_mask(path: str, target_hw: tuple) -> np.ndarray:
    """Load and binarize a mask image, resized to (height, width)."""
    m = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if m is None:
        raise FileNotFoundError(f"Cannot load mask: {path}")
    h, w = target_hw
    if m.shape != (h, w):
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
    _, m = cv2.threshold(m, 127, 255, cv2.THRESH_BINARY)
    return m


def resolve_device(value: str):
    """Convert device string to int if it's a digit (GPU index), else keep as str."""
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    return stripped


def resolve_video_source(value: str):
    """Convert numeric camera indexes to int, otherwise keep RTSP/file paths as strings."""
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    return value


def is_live_source(source) -> bool:
    if isinstance(source, int):
        return True
    value = str(source).lower()
    return value.startswith(("rtsp://", "rtmp://", "http://", "https://"))


def open_video_capture(source):
    if isinstance(source, str) and source.lower().startswith(("rtsp://", "rtmp://", "http://", "https://")):
        return cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    return cv2.VideoCapture(source)


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
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1, client_id)
    except AttributeError:
        client = mqtt_client.Client(client_id)
    except TypeError:
        client = mqtt_client.Client(client_id)
    return client


def connect_mqtt(args):
    if not args.mqtt_broker:
        return None

    client = create_mqtt_client(args.mqtt_client_id)
    if args.mqtt_username or args.mqtt_password:
        client.username_pw_set(args.mqtt_username, args.mqtt_password)

    def on_connect(mqtt, userdata, flags, rc):
        if rc == 0:
            print(f"Connected to MQTT broker {args.mqtt_broker}:{args.mqtt_port}")
            print(f"Publishing counts to topic: {args.mqtt_topic}")
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
        print(
            f"Could not connect to MQTT broker {args.mqtt_broker}:{args.mqtt_port}: {e}; "
            "continuing without MQTT publishing"
        )
        return None

    client.loop_start()
    return client


def publish_count_payload(mqtt_client, args, payload: dict):
    if mqtt_client is None:
        return
    message = json.dumps(payload, separators=(",", ":"))
    info = mqtt_client.publish(
        args.mqtt_topic,
        message,
        qos=args.mqtt_qos,
        retain=args.mqtt_retain,
    )
    if info.rc != 0:
        print(f"MQTT publish failed with rc={info.rc}")


def inside_mask_center(mask: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> bool:
    """Fast check: is the box center inside the mask?"""
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    cy = max(0, min(cy, mask.shape[0] - 1))
    cx = max(0, min(cx, mask.shape[1] - 1))
    return mask[cy, cx] > 0


def box_mask_overlap_ratio(mask: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> float:
    """Fraction of the bounding box area that falls inside the mask."""
    x1 = max(0, x1); y1 = max(0, y1)
    x2 = min(mask.shape[1], x2); y2 = min(mask.shape[0], y2)
    roi = mask[y1:y2, x1:x2]
    if roi.size == 0:
        return 0.0
    return float(np.count_nonzero(roi)) / roi.size


def in_mask(mask: np.ndarray, x1: int, y1: int, x2: int, y2: int,
            overlap_thresh: float) -> bool:
    """Return True if the detection box is sufficiently inside the mask."""
    if overlap_thresh <= 0.0:
        return inside_mask_center(mask, x1, y1, x2, y2)
    return box_mask_overlap_ratio(mask, x1, y1, x2, y2) >= overlap_thresh


def mask_roi_bounds(mask: np.ndarray | None, margin: int, frame_hw: tuple[int, int]) -> tuple[int, int, int, int] | None:
    if mask is None:
        return None
    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        return None
    height, width = frame_hw
    pad = max(0, int(margin))
    x1 = max(0, int(xs.min()) - pad)
    y1 = max(0, int(ys.min()) - pad)
    x2 = min(width, int(xs.max()) + pad + 1)
    y2 = min(height, int(ys.max()) + pad + 1)
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def make_dim_overlay(frame: np.ndarray, mask: np.ndarray,
                     alpha: float = 0.65) -> np.ndarray:
    """
    Build a darkened mask visualization from the current frame.
    Outside-mask pixels are dimmed; inside-mask pixels are kept bright.
    """
    dark = (frame * (1.0 - alpha)).astype(np.uint8)
    overlay = dark.copy()
    overlay[mask > 0] = frame[mask > 0]
    return overlay


def draw_mask_boundary(frame: np.ndarray, mask: np.ndarray,
                       color=(0, 200, 255), thickness=2) -> None:
    """Draw the mask contour on the frame in-place."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(frame, contours, -1, color, thickness)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    args = parse_args()
    device = resolve_device(args.device)
    source = resolve_video_source(args.source)
    counts_every = max(1, args.counts_every)
    mqtt_every = max(1, args.mqtt_every)
    live_source = is_live_source(source)
    read_failures_before_reconnect = max(1, args.read_failures_before_reconnect)
    reconnect_delay_initial = max(0.1, args.reconnect_delay)
    reconnect_delay_max = max(reconnect_delay_initial, args.max_reconnect_delay)

    # Load model — pass device at track() time, not model.to(), for TensorRT compatibility
    print(f"Loading model: {args.model}")
    model = load_yolo_model(args.model)

    # Open video source
    cap = open_video_capture(source)
    if not cap.isOpened():
        print(f"ERROR: Cannot open source: {args.source}")
        sys.exit(1)

    fps_src  = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Source: {width}x{height} @ {fps_src:.1f} fps")

    # Load mask once, resized to frame dimensions
    mask = None
    if args.mask:
        mask = load_mask(args.mask, target_hw=(height, width))
        n_mask_px = int(np.count_nonzero(mask))
        pct = 100.0 * n_mask_px / (width * height)
        print(f"Mask loaded: {n_mask_px} active pixels ({pct:.1f}% of frame)")
    roi_bounds = None
    if args.roi_from_mask:
        roi_bounds = mask_roi_bounds(mask, args.roi_margin, (height, width))
        if roi_bounds is None:
            print("ROI crop: disabled (mask missing or empty)")
        else:
            rx1, ry1, rx2, ry2 = roi_bounds
            roi_pct = 100.0 * ((rx2 - rx1) * (ry2 - ry1)) / (width * height)
            print(f"ROI crop: {rx2 - rx1}x{ry2 - ry1} at ({rx1},{ry1}) ({roi_pct:.1f}% of frame)")

    # Output writer
    writer = None
    if args.out:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(args.out, fourcc, max(1.0, fps_src), (width, height))
        print(f"Writing output to: {args.out}")

    # Count CSV writer. Flush each row so live runs keep usable data after interruption.
    counts_file = None
    counts_writer = None
    if args.counts_csv:
        counts_path = Path(args.counts_csv)
        counts_path.parent.mkdir(parents=True, exist_ok=True)
        counts_file = counts_path.open("w", newline="", encoding="utf-8")
        counts_writer = csv.writer(counts_file)
        counts_writer.writerow([
            "timestamp",
            "elapsed_seconds",
            "frame_index",
            "source_fps",
            "processing_fps",
            "raw_person_detections",
            "counted_persons",
            "counted_track_ids",
        ])
        counts_file.flush()
        print(f"Writing count CSV to: {counts_path}")

    # Rolling FPS window
    fps_window = deque(maxlen=30)
    last_time  = time.perf_counter()
    start_wall_time = time.time()

    print(f"Tracker : {'disabled (detector-only)' if args.no_tracker else args.tracker}")
    print(f"Device  : {device}")
    print(f"Conf    : {args.conf}  |  IoU NMS: {args.iou}  |  imgsz: {args.imgsz}")
    print(f"Latest JPEG: {args.latest_jpeg or 'disabled'}")
    mqtt_client = connect_mqtt(args)
    print(f"Mask filter: {'overlap≥' + str(args.mask_overlap) if args.mask_overlap > 0 else 'center-point'}")
    print("Running — press Ctrl+C or 'q' to stop.\n")

    frame_idx   = 0
    person_counts = []  # track count per frame, useful for analytics
    consecutive_read_failures = 0
    reconnect_delay = reconnect_delay_initial

    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                if not live_source:
                    print("Stream ended or frame read failed.")
                    break

                consecutive_read_failures += 1
                if consecutive_read_failures < read_failures_before_reconnect:
                    time.sleep(0.2)
                    continue

                print(
                    f"Live stream read failed {consecutive_read_failures} consecutive time(s); "
                    f"reconnecting in {reconnect_delay:.1f}s..."
                )
                cap.release()

                while True:
                    time.sleep(reconnect_delay)
                    cap = open_video_capture(source)
                    if cap.isOpened():
                        new_fps = cap.get(cv2.CAP_PROP_FPS) or fps_src or 25.0
                        new_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        new_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        if new_width > 0 and new_height > 0 and (new_width != width or new_height != height):
                            width = new_width
                            height = new_height
                            if args.mask:
                                mask = load_mask(args.mask, target_hw=(height, width))
                                n_mask_px = int(np.count_nonzero(mask))
                                pct = 100.0 * n_mask_px / (width * height)
                                print(f"Mask reloaded: {n_mask_px} active pixels ({pct:.1f}% of frame)")
                            if args.roi_from_mask:
                                roi_bounds = mask_roi_bounds(mask, args.roi_margin, (height, width))
                                if roi_bounds is None:
                                    print("ROI crop: disabled after reconnect (mask missing or empty)")
                                else:
                                    rx1, ry1, rx2, ry2 = roi_bounds
                                    roi_pct = 100.0 * ((rx2 - rx1) * (ry2 - ry1)) / (width * height)
                                    print(
                                        f"ROI crop reloaded: {rx2 - rx1}x{ry2 - ry1} "
                                        f"at ({rx1},{ry1}) ({roi_pct:.1f}% of frame)"
                                    )
                            if writer:
                                writer.release()
                                writer = cv2.VideoWriter(
                                    args.out,
                                    fourcc,
                                    max(1.0, new_fps),
                                    (width, height),
                                )
                        fps_src = new_fps
                        consecutive_read_failures = 0
                        reconnect_delay = reconnect_delay_initial
                        print(f"Reconnected to live source: {width}x{height} @ {fps_src:.1f} fps")
                        break

                    cap.release()
                    next_delay = min(reconnect_delay * 2.0, reconnect_delay_max)
                    print(f"Reconnect failed; retrying in {next_delay:.1f}s...")
                    reconnect_delay = next_delay
                continue

            consecutive_read_failures = 0
            reconnect_delay = reconnect_delay_initial
            frame_idx += 1

            # ── YOLO tracking ────────────────────────────────────────────────
            inference_frame = frame
            roi_offset_x = 0
            roi_offset_y = 0
            if roi_bounds is not None:
                rx1, ry1, rx2, ry2 = roi_bounds
                inference_frame = frame[ry1:ry2, rx1:rx2]
                roi_offset_x = rx1
                roi_offset_y = ry1

            if args.no_tracker:
                results = model.predict(
                    source=inference_frame,  # variable, NOT the string "frame"
                    conf=args.conf,
                    iou=args.iou,
                    classes=[0],             # person only
                    device=device,
                    imgsz=args.imgsz,
                    verbose=False,
                )
            else:
                # persist=True is critical — keeps tracker state between frames
                results = model.track(
                    source=inference_frame,  # variable, NOT the string "frame"
                    conf=args.conf,
                    iou=args.iou,
                    classes=[0],             # person only
                    device=device,
                    imgsz=args.imgsz,
                    tracker=args.tracker,
                    persist=True,            # tracker state survives across frames
                    verbose=False,
                )

            # ── Post-detection mask filtering ─────────────────────────────────
            vis_frame = frame.copy()

            # Apply mask visualization from the live frame so the non-ROI
            # background remains current instead of freezing at the first frame.
            if args.show_mask and mask is not None:
                vis_frame = make_dim_overlay(frame, mask, alpha=0.65)

            r = results[0]
            person_count = 0
            raw_person_detections = 0
            counted_track_ids = []

            if r.boxes is not None:
                boxes_xyxy = r.boxes.xyxy.cpu().numpy().astype(int)
                raw_person_detections = len(boxes_xyxy)
                if roi_bounds is not None and raw_person_detections > 0:
                    boxes_xyxy[:, [0, 2]] += roi_offset_x
                    boxes_xyxy[:, [1, 3]] += roi_offset_y
                    boxes_xyxy[:, [0, 2]] = np.clip(boxes_xyxy[:, [0, 2]], 0, width - 1)
                    boxes_xyxy[:, [1, 3]] = np.clip(boxes_xyxy[:, [1, 3]], 0, height - 1)
                if r.boxes.id is not None:
                    track_ids = r.boxes.id.cpu().numpy().astype(int).tolist()
                else:
                    track_ids = [None] * raw_person_detections
                if r.boxes.conf is not None:
                    confs = r.boxes.conf.cpu().numpy()
                else:
                    confs = np.ones(raw_person_detections)

                for (x1, y1, x2, y2), tid, conf in zip(boxes_xyxy, track_ids, confs):

                    # ── Mask filter — zero image cost ─────────────────────────
                    if mask is not None:
                        if not in_mask(mask, x1, y1, x2, y2, args.mask_overlap):
                            continue  # outside active region, skip

                    person_count += 1
                    if tid is not None:
                        counted_track_ids.append(str(tid))

                    # Draw bounding box
                    cv2.rectangle(vis_frame, (x1, y1), (x2, y2), (10, 255, 10), 2)

                    # Label: ID + confidence
                    label = f"ID {tid}  {conf:.2f}" if tid is not None else f"person  {conf:.2f}"
                    label_y = max(20, y1 - 6)
                    (lw, lh), baseline = cv2.getTextSize(
                        label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
                    cv2.rectangle(vis_frame,
                                  (x1, label_y - lh - baseline),
                                  (x1 + lw, label_y + baseline),
                                  (10, 255, 10), cv2.FILLED)
                    cv2.putText(vis_frame, label, (x1, label_y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)

            person_counts.append(person_count)

            # Draw mask boundary contour
            if mask is not None and args.show_mask:
                draw_mask_boundary(vis_frame, mask)

            # ── Rolling FPS ───────────────────────────────────────────────────
            now = time.perf_counter()
            fps_window.append(1.0 / max(1e-6, now - last_time))
            last_time = now
            avg_fps = sum(fps_window) / len(fps_window)

            cv2.putText(vis_frame, f"FPS: {avg_fps:.1f}  Persons: {person_count}",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            write_latest_jpeg(vis_frame, args.latest_jpeg)

            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elapsed_seconds = round(time.time() - start_wall_time, 3)
            source_fps_value = round(float(fps_src), 3)
            processing_fps_value = round(float(avg_fps), 3)
            count_payload = {
                "timestamp": timestamp_str,
                "elapsed_seconds": elapsed_seconds,
                "frame_index": frame_idx,
                "source": args.source,
                "source_fps": source_fps_value,
                "processing_fps": processing_fps_value,
                "raw_person_detections": raw_person_detections,
                "counted_persons": person_count,
                "counted_track_ids": counted_track_ids,
                "mask": args.mask or "",
                "roi": list(roi_bounds) if roi_bounds is not None else None,
                "model": args.model,
            }

            if counts_writer is not None and frame_idx % counts_every == 0:
                counts_writer.writerow([
                    timestamp_str,
                    elapsed_seconds,
                    frame_idx,
                    source_fps_value,
                    processing_fps_value,
                    raw_person_detections,
                    person_count,
                    "|".join(counted_track_ids),
                ])
                counts_file.flush()

            if mqtt_client is not None and frame_idx % mqtt_every == 0:
                publish_count_payload(mqtt_client, args, count_payload)

            # ── Display ───────────────────────────────────────────────────────
            if args.display:
                cv2.imshow("Person Tracker", vis_frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("'q' pressed, stopping.")
                    break

            if writer:
                writer.write(vis_frame)

            # Console log every 100 frames
            if frame_idx % 100 == 0:
                print(f"  Frame {frame_idx:6d} | FPS {avg_fps:5.1f} | Persons in mask: {person_count}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

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

        if person_counts:
            print(f"\n── Session summary ──────────────────────")
            print(f"  Total frames processed : {frame_idx}")
            print(f"  Avg persons detected   : {sum(person_counts)/len(person_counts):.2f}")
            print(f"  Max persons in frame   : {max(person_counts)}")
        print("Done.")


if __name__ == "__main__":
    main()
