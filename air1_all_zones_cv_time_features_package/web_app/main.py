from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse as _BaseJSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


def _clean_for_json(value: Any) -> Any:
    """Recursively replace NaN / +-Inf floats with None so json.dumps(allow_nan=False) is happy.

    Starlette's JSONResponse renders with allow_nan=False (correct per the JSON
    spec). Any non-finite float buried in a payload, typically from an error
    tick's probability or an unset metric, would otherwise raise a 500.
    """
    if isinstance(value, dict):
        return {key: _clean_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_for_json(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


class JSONResponse(_BaseJSONResponse):
    def render(self, content: Any) -> bytes:
        return super().render(_clean_for_json(content))

WEB_APP_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = WEB_APP_DIR.parent
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

from air1_all_zones import model as training  # noqa: E402

from web_app.cv_ground_truth import CvGroundTruthTailer  # noqa: E402
from web_app.data_source import build_data_source_from_env  # noqa: E402
from web_app.inference_loop import InferenceConfig, InferenceLoop  # noqa: E402
from web_app.rtsp_stream import DisabledFrameGrabber, FileBackedMjpegGrabber, RtspFrameGrabber  # noqa: E402

try:
    from dotenv import load_dotenv  # type: ignore[import]

    skip_dotenv = os.environ.get("AIR1_ALL_ZONES_SKIP_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}
    if not skip_dotenv:
        load_dotenv(WEB_APP_DIR / ".env")
except ImportError:
    pass

logger = logging.getLogger("air1_all_zones_web_app")
logging.basicConfig(level=os.environ.get("AIR1_ALL_ZONES_LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

PRODUCTION_POINTER_FILENAME = "production_run.txt"
DEFAULT_PRODUCTION_POINTER = training.DEFAULT_OUTPUT_DIR / PRODUCTION_POINTER_FILENAME
DEFAULT_CV_GROUND_TRUTH_TABLE = WORKSPACE_DIR / "data" / "cv_occupancy_all_air1_10sec.csv"
DEFAULT_ANNOTATED_FRAME_MAX_AGE_SEC = 30.0
DEFAULT_MJPEG_TARGET_FPS = 15.0
CAMERA_CONFIGS = {
    "cam1": {
        "label": "cam1",
        "env": "AIR1_ALL_ZONES_RTSP_URL_CAM1",
        "annotated_env": "AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM1",
        "annotated_default": "data/yolo_latest_cam1.jpg",
        "host": "10.158.71.241",
        "coverage_zones": [4, 9, 10, 11, 12, 13, 14, 15],
        "excluded_zones": [16],
    },
    "cam2": {
        "label": "cam2",
        "env": "AIR1_ALL_ZONES_RTSP_URL_CAM2",
        "annotated_env": "AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM2",
        "annotated_default": "data/yolo_latest_cam2.jpg",
        "host": "10.158.71.240",
        "coverage_zones": [1, 2, 3, 5, 6, 7, 8],
        "excluded_zones": [],
    },
}
FrameGrabber = RtspFrameGrabber | FileBackedMjpegGrabber | DisabledFrameGrabber


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    p = Path(raw)
    if not p.is_absolute():
        p = (WORKSPACE_DIR / p).resolve()
    return p


def _resolve_optional_path(raw: str) -> Path | None:
    value = raw.strip()
    if not value:
        return None
    if value.lower() in {"none", "off", "disabled", "false", "0"}:
        return None
    p = Path(value)
    if not p.is_absolute():
        p = (WORKSPACE_DIR / p).resolve()
    return p


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} is not a valid float: {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} is not a valid int: {raw!r}") from exc


def _env_optional_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw or raw.lower() in {"none", "off", "disabled"}:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} is not a valid float or 'none': {raw!r}") from exc


def _build_inference_config() -> InferenceConfig:
    return InferenceConfig(
        production_pointer=_env_path("AIR1_ALL_ZONES_PRODUCTION_POINTER", DEFAULT_PRODUCTION_POINTER),
        tick_interval_sec=_env_float("AIR1_ALL_ZONES_TICK_INTERVAL_SEC", 10.0),
        history_size=_env_int("AIR1_ALL_ZONES_HISTORY_SIZE", 240),
        max_gap_minutes=_env_float("AIR1_ALL_ZONES_MAX_GAP_MINUTES", 5.0),
        max_age_minutes=_env_optional_float("AIR1_ALL_ZONES_MAX_AGE_MINUTES") if "AIR1_ALL_ZONES_MAX_AGE_MINUTES" in os.environ else 15.0,
        threshold=_env_optional_float("AIR1_ALL_ZONES_OCCUPIED_THRESHOLD"),
        pointer_poll_interval_sec=_env_float("AIR1_ALL_ZONES_POINTER_POLL_SEC", 30.0),
    )


def _build_ground_truth_source() -> CvGroundTruthTailer | None:
    enabled_raw = os.environ.get("AIR1_ALL_ZONES_CV_GROUND_TRUTH_ENABLED", "true").strip().lower()
    if enabled_raw in {"0", "false", "no", "off", "disabled"}:
        return None
    if "AIR1_ALL_ZONES_CV_GROUND_TRUTH_PARQUET" in os.environ and "AIR1_ALL_ZONES_CV_GROUND_TRUTH_TABLE" not in os.environ:
        return CvGroundTruthTailer(
            _env_path("AIR1_ALL_ZONES_CV_GROUND_TRUTH_PARQUET", DEFAULT_CV_GROUND_TRUTH_TABLE)
        )
    return CvGroundTruthTailer(
        _env_path("AIR1_ALL_ZONES_CV_GROUND_TRUTH_TABLE", DEFAULT_CV_GROUND_TRUTH_TABLE)
    )


def _camera_rtsp_url(camera_id: str) -> str:
    camera_config = CAMERA_CONFIGS[camera_id]
    return os.environ.get(str(camera_config["env"]), "").strip()


def _camera_annotated_frame_path(camera_id: str) -> Path | None:
    camera_config = CAMERA_CONFIGS[camera_id]
    env_name = str(camera_config["annotated_env"])
    raw = os.environ.get(env_name)
    if raw is None:
        return (WORKSPACE_DIR / str(camera_config["annotated_default"])).resolve()
    return _resolve_optional_path(raw)


def _build_rtsp_grabbers() -> dict[str, FrameGrabber]:
    max_age_sec = _env_float("AIR1_ALL_ZONES_ANNOTATED_FRAME_MAX_AGE_SEC", DEFAULT_ANNOTATED_FRAME_MAX_AGE_SEC)
    grabbers: dict[str, FrameGrabber] = {}
    for camera_id in CAMERA_CONFIGS:
        annotated_path = _camera_annotated_frame_path(camera_id)
        if annotated_path is not None:
            grabbers[camera_id] = FileBackedMjpegGrabber(annotated_path, max_age_sec=max_age_sec)
            continue
        rtsp_url = _camera_rtsp_url(camera_id)
        if rtsp_url:
            grabbers[camera_id] = RtspFrameGrabber(rtsp_url=rtsp_url)
        else:
            grabbers[camera_id] = DisabledFrameGrabber(f"{camera_id} video disabled")
    return grabbers


def _build_mjpeg_target_fps() -> float:
    return max(1.0, _env_float("AIR1_ALL_ZONES_MJPEG_TARGET_FPS", DEFAULT_MJPEG_TARGET_FPS))


def _rtsp_status(camera_id: str, grabber: FrameGrabber) -> dict[str, Any]:
    camera_config = CAMERA_CONFIGS[camera_id]
    return {
        "camera_id": camera_id,
        "label": camera_config["label"],
        "host": camera_config["host"],
        "coverage_zones": list(camera_config["coverage_zones"]),
        "excluded_zones": list(camera_config["excluded_zones"]),
        "url_safe": grabber.public_url,
        "connected": grabber.connected,
        "latest_frame_age_sec": grabber.latest_frame_age,
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    data_source = build_data_source_from_env(dict(os.environ))
    config = _build_inference_config()
    ground_truth_source = _build_ground_truth_source()
    mjpeg_target_fps = _build_mjpeg_target_fps()
    inference_loop = InferenceLoop(
        config=config,
        data_source=data_source,
        ground_truth_source=ground_truth_source,
    )
    rtsp_grabbers = _build_rtsp_grabbers()

    for camera_id, rtsp_grabber in rtsp_grabbers.items():
        logger.info("Starting %s video grabber for %s", camera_id, rtsp_grabber.public_url)
        rtsp_grabber.start()
    logger.info("Starting inference loop: data_source=%s tick=%.1fs lookback_pointer=%s",
                data_source.label, config.tick_interval_sec, config.production_pointer)
    inference_task = asyncio.create_task(inference_loop.run(), name="inference-loop")

    app.state.inference_loop = inference_loop
    app.state.rtsp_grabbers = rtsp_grabbers
    app.state.rtsp_grabber = rtsp_grabbers["cam1"]
    app.state.data_source = data_source
    app.state.ground_truth_source = ground_truth_source
    app.state.config = config
    app.state.mjpeg_target_fps = mjpeg_target_fps
    app.state.inference_task = inference_task

    try:
        yield
    finally:
        logger.info("Shutting down inference loop and RTSP grabbers")
        inference_loop.stop()
        try:
            await asyncio.wait_for(inference_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            inference_task.cancel()
        for rtsp_grabber in rtsp_grabbers.values():
            rtsp_grabber.stop()


app = FastAPI(title="AIR-1 All-Zones Live Occupancy Web App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_APP_DIR / "static"), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(WEB_APP_DIR / "static" / "index.html")


@app.get("/api/health")
async def health(request: Request) -> JSONResponse:
    inference_loop: InferenceLoop = request.app.state.inference_loop
    rtsp_grabbers: dict[str, FrameGrabber] = request.app.state.rtsp_grabbers
    config: InferenceConfig = request.app.state.config
    rtsp_by_camera = {
        camera_id: _rtsp_status(camera_id, grabber)
        for camera_id, grabber in rtsp_grabbers.items()
    }
    payload = {
        "ok": True,
        "rtsp": rtsp_by_camera["cam1"],
        "rtsp_by_camera": rtsp_by_camera,
        "inference": inference_loop.status(),
        "config": {
            "tick_interval_sec": config.tick_interval_sec,
            "history_size": config.history_size,
            "max_gap_minutes": config.max_gap_minutes,
            "max_age_minutes": config.max_age_minutes,
            "threshold": config.threshold,
            "mjpeg_target_fps": getattr(request.app.state, "mjpeg_target_fps", DEFAULT_MJPEG_TARGET_FPS),
            "production_pointer": str(config.production_pointer),
            "cv_ground_truth_table": (
                str(request.app.state.ground_truth_source.table_path)
                if request.app.state.ground_truth_source
                else None
            ),
            "camera_coverage": {
                camera_id: {
                    "host": config_payload["host"],
                    "coverage_zones": list(config_payload["coverage_zones"]),
                    "excluded_zones": list(config_payload["excluded_zones"]),
                }
                for camera_id, config_payload in CAMERA_CONFIGS.items()
            },
        },
    }
    return JSONResponse(payload)


@app.get("/api/current")
async def current(request: Request) -> JSONResponse:
    inference_loop: InferenceLoop = request.app.state.inference_loop
    if not inference_loop.history:
        raise HTTPException(status_code=404, detail="no events yet")
    return JSONResponse(inference_loop.history[-1].to_dict())


@app.get("/api/history")
async def history(request: Request, n: int = 240) -> JSONResponse:
    inference_loop: InferenceLoop = request.app.state.inference_loop
    n = max(1, min(int(n), inference_loop.config.history_size))
    events = list(inference_loop.history)[-n:]
    return JSONResponse([event.to_dict() for event in events])


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    inference_loop: InferenceLoop = request.app.state.inference_loop
    queue = await inference_loop.subscribe()

    async def event_iterator() -> AsyncIterator[bytes]:
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                payload = json.dumps(event.to_dict(), default=str)
                yield f"event: tick\ndata: {payload}\n\n".encode("utf-8")
        finally:
            await inference_loop.unsubscribe(queue)

    return StreamingResponse(event_iterator(), media_type="text/event-stream")


def _video_response(rtsp_grabber: FrameGrabber, target_fps: float) -> StreamingResponse:
    boundary = "frame"

    async def mjpeg_generator() -> AsyncIterator[bytes]:
        iterator = rtsp_grabber.mjpeg_iterator(target_fps=target_fps)
        while True:
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            yield chunk
            await asyncio.sleep(0)

    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    return StreamingResponse(
        mjpeg_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers=headers,
    )


@app.get("/api/video/{camera_id}.mjpg")
async def video_by_camera(request: Request, camera_id: str) -> StreamingResponse:
    rtsp_grabbers: dict[str, FrameGrabber] = request.app.state.rtsp_grabbers
    if camera_id not in rtsp_grabbers:
        raise HTTPException(status_code=404, detail=f"unknown camera: {camera_id}")
    target_fps = float(getattr(request.app.state, "mjpeg_target_fps", DEFAULT_MJPEG_TARGET_FPS))
    return _video_response(rtsp_grabbers[camera_id], target_fps)


@app.get("/api/video.mjpg")
async def video(request: Request) -> StreamingResponse:
    rtsp_grabber: FrameGrabber = request.app.state.rtsp_grabber
    target_fps = float(getattr(request.app.state, "mjpeg_target_fps", DEFAULT_MJPEG_TARGET_FPS))
    return _video_response(rtsp_grabber, target_fps)


