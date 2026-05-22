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
    spec). Any non-finite float buried in a payload — typically from an error
    tick's probability or an unset metric — would otherwise raise a 500.
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

from zone5 import model as training  # noqa: E402

from web_app.cv_ground_truth import CvGroundTruthTailer  # noqa: E402
from web_app.data_source import build_data_source_from_env  # noqa: E402
from web_app.inference_loop import InferenceConfig, InferenceLoop  # noqa: E402
from web_app.rtsp_stream import DisabledFrameGrabber, FileBackedMjpegGrabber, RtspFrameGrabber  # noqa: E402

try:
    from dotenv import load_dotenv  # type: ignore[import]

    skip_dotenv = os.environ.get("ZONE5_SKIP_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}
    if not skip_dotenv:
        load_dotenv(WEB_APP_DIR / ".env")
except ImportError:
    pass

logger = logging.getLogger("zone5_web_app")
logging.basicConfig(level=os.environ.get("ZONE5_LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

PRODUCTION_POINTER_FILENAME = "production_run.txt"
DEFAULT_PRODUCTION_POINTER = training.DEFAULT_OUTPUT_DIR / PRODUCTION_POINTER_FILENAME
DEFAULT_CV_GROUND_TRUTH_TABLE = WORKSPACE_DIR / "data" / "cv_occupancy_zone5_10sec.csv"
DEFAULT_ANNOTATED_FRAME_PATH = WORKSPACE_DIR / "data" / "yolo_latest.jpg"
DEFAULT_ANNOTATED_FRAME_MAX_AGE_SEC = 30.0
DEFAULT_MASK_PATH = WORKSPACE_DIR / "cv_counter" / "masks" / "cam1-desk5-mask.png"
DEFAULT_MJPEG_TARGET_FPS = 15.0
FrameGrabber = RtspFrameGrabber | FileBackedMjpegGrabber | DisabledFrameGrabber


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    p = Path(raw)
    if not p.is_absolute():
        p = (WORKSPACE_DIR / p).resolve()
    return p


def _env_optional_path(name: str, default: Path | None = None) -> Path | None:
    raw = os.environ.get(name)
    if raw is None:
        return default.resolve() if default is not None else None
    value = raw.strip()
    if not value or value.lower() in {"none", "off", "disabled", "false", "0"}:
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
        production_pointer=_env_path("ZONE5_PRODUCTION_POINTER", DEFAULT_PRODUCTION_POINTER),
        tick_interval_sec=_env_float("ZONE5_TICK_INTERVAL_SEC", 10.0),
        history_size=_env_int("ZONE5_HISTORY_SIZE", 240),
        max_gap_minutes=_env_float("ZONE5_MAX_GAP_MINUTES", 5.0),
        max_age_minutes=_env_optional_float("ZONE5_MAX_AGE_MINUTES") if "ZONE5_MAX_AGE_MINUTES" in os.environ else 15.0,
        threshold=_env_optional_float("ZONE5_OCCUPIED_THRESHOLD"),
        pointer_poll_interval_sec=_env_float("ZONE5_POINTER_POLL_SEC", 30.0),
    )


def _build_ground_truth_source() -> CvGroundTruthTailer | None:
    enabled_raw = os.environ.get("ZONE5_CV_GROUND_TRUTH_ENABLED", "true").strip().lower()
    if enabled_raw in {"0", "false", "no", "off", "disabled"}:
        return None
    if "ZONE5_CV_GROUND_TRUTH_PARQUET" in os.environ and "ZONE5_CV_GROUND_TRUTH_TABLE" not in os.environ:
        return CvGroundTruthTailer(
            _env_path("ZONE5_CV_GROUND_TRUTH_PARQUET", DEFAULT_CV_GROUND_TRUTH_TABLE)
        )
    return CvGroundTruthTailer(
        _env_path("ZONE5_CV_GROUND_TRUTH_TABLE", DEFAULT_CV_GROUND_TRUTH_TABLE)
    )


def _build_video_grabber() -> FrameGrabber:
    annotated_path = _env_optional_path("ZONE5_ANNOTATED_FRAME_PATH", DEFAULT_ANNOTATED_FRAME_PATH)
    if annotated_path is not None:
        max_age_sec = _env_float("ZONE5_ANNOTATED_FRAME_MAX_AGE_SEC", DEFAULT_ANNOTATED_FRAME_MAX_AGE_SEC)
        return FileBackedMjpegGrabber(annotated_path, max_age_sec=max_age_sec)
    rtsp_url = os.environ.get("ZONE5_RTSP_URL", "").strip()
    if rtsp_url:
        return RtspFrameGrabber(rtsp_url=rtsp_url)
    return DisabledFrameGrabber("Video disabled")


def _build_mask_path() -> Path | None:
    return _env_optional_path("ZONE5_MASK_PATH", DEFAULT_MASK_PATH)


def _build_mjpeg_target_fps() -> float:
    return max(1.0, _env_float("ZONE5_MJPEG_TARGET_FPS", DEFAULT_MJPEG_TARGET_FPS))


def _mask_status(mask_path: Path | None) -> dict[str, Any]:
    available = bool(mask_path and mask_path.is_file())
    return {
        "available": available,
        "path": str(mask_path) if mask_path is not None else None,
        "url": "/api/mask.png" if available else None,
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
    rtsp_grabber = _build_video_grabber()
    mask_path = _build_mask_path()

    logger.info("Starting video grabber for %s", rtsp_grabber.public_url)
    rtsp_grabber.start()
    logger.info("Mask overlay path: %s", mask_path if mask_path is not None else "disabled")
    logger.info("Starting inference loop: data_source=%s tick=%.1fs lookback_pointer=%s",
                data_source.label, config.tick_interval_sec, config.production_pointer)
    inference_task = asyncio.create_task(inference_loop.run(), name="inference-loop")

    app.state.inference_loop = inference_loop
    app.state.rtsp_grabber = rtsp_grabber
    app.state.data_source = data_source
    app.state.ground_truth_source = ground_truth_source
    app.state.config = config
    app.state.mask_path = mask_path
    app.state.mjpeg_target_fps = mjpeg_target_fps
    app.state.inference_task = inference_task

    try:
        yield
    finally:
        logger.info("Shutting down inference loop and RTSP grabber")
        inference_loop.stop()
        try:
            await asyncio.wait_for(inference_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            inference_task.cancel()
        rtsp_grabber.stop()


app = FastAPI(title="Zone 5 Live Occupancy Web App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_APP_DIR / "static"), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(WEB_APP_DIR / "static" / "index.html")


@app.get("/api/health")
async def health(request: Request) -> JSONResponse:
    inference_loop: InferenceLoop = request.app.state.inference_loop
    rtsp_grabber: FrameGrabber = request.app.state.rtsp_grabber
    config: InferenceConfig = request.app.state.config
    payload = {
        "ok": True,
        "rtsp": {
            "url_safe": rtsp_grabber.public_url,
            "connected": rtsp_grabber.connected,
            "latest_frame_age_sec": rtsp_grabber.latest_frame_age,
        },
        "mask": _mask_status(getattr(request.app.state, "mask_path", _build_mask_path())),
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
        },
    }
    return JSONResponse(payload)


@app.get("/api/mask.png")
async def mask_image(request: Request) -> FileResponse:
    mask_path = getattr(request.app.state, "mask_path", _build_mask_path())
    if mask_path is None or not mask_path.is_file():
        raise HTTPException(status_code=404, detail="mask image is not available")
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    return FileResponse(mask_path, media_type="image/png", headers=headers)


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


@app.get("/api/video.mjpg")
async def video(request: Request) -> StreamingResponse:
    rtsp_grabber: FrameGrabber = request.app.state.rtsp_grabber
    target_fps = float(getattr(request.app.state, "mjpeg_target_fps", DEFAULT_MJPEG_TARGET_FPS))
    boundary = "frame"

    def next_chunk(iterator: Any) -> bytes | None:
        try:
            return next(iterator)
        except StopIteration:
            return None

    async def mjpeg_generator() -> AsyncIterator[bytes]:
        iterator = rtsp_grabber.mjpeg_iterator(target_fps=target_fps)
        while True:
            chunk = await asyncio.to_thread(next_chunk, iterator)
            if chunk is None:
                break
            yield chunk

    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    return StreamingResponse(
        mjpeg_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers=headers,
    )
