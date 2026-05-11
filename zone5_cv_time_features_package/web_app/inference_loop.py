from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

WEB_APP_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = WEB_APP_DIR.parent
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

from zone5 import model as training  # noqa: E402

from web_app.cv_ground_truth import CvGroundTruthTailer  # noqa: E402
from web_app.data_source import DataSource  # noqa: E402


@dataclass
class ModelState:
    run_dir: Path | None = None
    run_id: str | None = None
    pointer_mtime: float = 0.0
    scaler_stats: dict[str, Any] | None = None
    best_params_payload: dict[str, Any] | None = None
    lookback: int | None = None
    loaded_at: float = 0.0


def _json_safe_float(value: Any) -> float | None:
    """Coerce to a JSON-compliant float, mapping NaN / +-Inf / non-numbers to None.

    Starlette's JSONResponse encodes with allow_nan=False, so any NaN in a
    response payload (e.g., from an error tick) raises ValueError.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


@dataclass
class TickEvent:
    timestamp: str
    probability: float
    occupied: bool | None
    model_run_id: str | None
    reference_time: str
    source_label: str
    ground_truth_count: float | int | None = None
    ground_truth_occupied: bool | None = None
    ground_truth_timestamp: str | None = None
    ground_truth_age_minutes: float | int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "probability": _json_safe_float(self.probability),
            "occupied": self.occupied,
            "model_run_id": self.model_run_id,
            "reference_time": self.reference_time,
            "source_label": self.source_label,
            "ground_truth_count": _json_safe_float(self.ground_truth_count),
            "ground_truth_occupied": self.ground_truth_occupied,
            "ground_truth_timestamp": self.ground_truth_timestamp,
            "ground_truth_age_minutes": _json_safe_float(self.ground_truth_age_minutes),
            "error": self.error,
        }


@dataclass
class InferenceConfig:
    production_pointer: Path
    tick_interval_sec: float
    history_size: int
    max_gap_minutes: float
    max_age_minutes: float | None
    threshold: float | None
    pointer_poll_interval_sec: float = 30.0


def _resolve_pointer(pointer_path: Path) -> Path | None:
    if not pointer_path.is_file():
        return None
    raw = pointer_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"production pointer is empty; no model has been promoted yet: {pointer_path}")
    target = Path(raw)
    if not target.is_absolute():
        target = (WORKSPACE_DIR / target).resolve()
    return target


def _resolve_run_id_for_dir(run_dir: Path) -> str | None:
    manifest_path = run_dir / training.RUN_MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8")).get("run_id")
        except (OSError, json.JSONDecodeError):
            return None
    return None


class InferenceLoop:
    def __init__(
        self,
        config: InferenceConfig,
        data_source: DataSource,
        ground_truth_source: CvGroundTruthTailer | None = None,
    ) -> None:
        self.config = config
        self.data_source = data_source
        self.ground_truth_source = ground_truth_source
        self.history: deque[TickEvent] = deque(maxlen=config.history_size)
        self.state = ModelState()
        self._subscribers: list[asyncio.Queue[TickEvent]] = []
        self._subscribers_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._last_pointer_check: float = 0.0
        self._last_error: str | None = None

    async def subscribe(self) -> asyncio.Queue[TickEvent]:
        queue: asyncio.Queue[TickEvent] = asyncio.Queue(maxsize=64)
        async with self._subscribers_lock:
            self._subscribers.append(queue)
        for event in list(self.history):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                break
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[TickEvent]) -> None:
        async with self._subscribers_lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass

    async def _broadcast(self, event: TickEvent) -> None:
        async with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def _load_artifacts_blocking(self, target_dir: Path) -> ModelState | None:
        try:
            scaler_stats, best_params_payload, _checkpoint = training._load_artifacts(target_dir)
            training.require_10_second_model_contract(scaler_stats, best_params_payload, _checkpoint)
        except (FileNotFoundError, ValueError) as exc:
            self._last_error = f"model load failed: {exc}"
            return None
        new_state = ModelState(
            run_dir=target_dir,
            run_id=_resolve_run_id_for_dir(target_dir) or target_dir.name,
            pointer_mtime=self.state.pointer_mtime,
            scaler_stats=scaler_stats,
            best_params_payload=best_params_payload,
            lookback=int(scaler_stats.get("lookback_rows") or scaler_stats["lookback"]),
            loaded_at=time.time(),
        )
        return new_state

    async def _maybe_reload_model(self) -> None:
        pointer_path = self.config.production_pointer
        if not pointer_path.is_file():
            if self.state.run_dir is None:
                self._last_error = f"production pointer missing: {pointer_path}"
            return
        try:
            mtime = pointer_path.stat().st_mtime
        except OSError as exc:
            self._last_error = f"cannot stat production pointer: {exc}"
            return
        if self.state.run_dir is not None and mtime <= self.state.pointer_mtime:
            return
        try:
            target = _resolve_pointer(pointer_path)
        except ValueError as exc:
            self._last_error = str(exc)
            return
        if target is None:
            self._last_error = f"production pointer missing: {pointer_path}; no model has been promoted yet"
            return
        new_state = await asyncio.to_thread(self._load_artifacts_blocking, target)
        if new_state is None:
            return
        new_state.pointer_mtime = mtime
        self.state = new_state
        self._last_error = None

    def _predict_blocking(self) -> tuple[pd.DataFrame, pd.Timestamp, float]:
        if self.state.run_dir is None or self.state.lookback is None:
            raise RuntimeError("no model loaded yet")
        window, latest_ts = self.data_source.get_latest_window(self.state.lookback)
        probability = training.predict_zone_5_probability(
            window,
            artifact_dir=self.state.run_dir,
            reference_time=latest_ts,
            max_gap_minutes=self.config.max_gap_minutes,
            max_age_minutes=self.config.max_age_minutes,
        )
        return window, latest_ts, float(probability)

    async def _tick_once(self) -> None:
        if self.state.run_dir is None:
            return
        try:
            _window, latest_ts, probability = await asyncio.to_thread(self._predict_blocking)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            self._last_error = error_text
            event = TickEvent(
                timestamp=pd.Timestamp.now(tz=None).isoformat(),
                probability=float("nan"),
                occupied=None,
                model_run_id=self.state.run_id,
                reference_time="",
                source_label=self.data_source.label,
                **self._ground_truth_fields(pd.Timestamp.now(tz=None)),
                error=error_text,
            )
            self.history.append(event)
            await self._broadcast(event)
            return
        if not math.isfinite(probability):
            return
        threshold = self.config.threshold
        occupied = bool(probability >= threshold) if threshold is not None else None
        ground_truth_fields = await asyncio.to_thread(self._ground_truth_fields, latest_ts)
        event = TickEvent(
            timestamp=pd.Timestamp(latest_ts).isoformat(),
            probability=probability,
            occupied=occupied,
            model_run_id=self.state.run_id,
            reference_time=pd.Timestamp(latest_ts).isoformat(),
            source_label=self.data_source.label,
            **ground_truth_fields,
            error=None,
        )
        self.history.append(event)
        await self._broadcast(event)
        self._last_error = None

    async def run(self) -> None:
        while not self._stop_event.is_set():
            now_mono = time.monotonic()
            if now_mono - self._last_pointer_check >= self.config.pointer_poll_interval_sec:
                await self._maybe_reload_model()
                self._last_pointer_check = now_mono
            else:
                if self.state.run_dir is None:
                    await self._maybe_reload_model()
            await self._tick_once()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.tick_interval_sec)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()

    def _ground_truth_fields(self, reference_time: Any | None) -> dict[str, Any]:
        if self.ground_truth_source is None:
            return {
                "ground_truth_count": None,
                "ground_truth_occupied": None,
                "ground_truth_timestamp": None,
                "ground_truth_age_minutes": None,
            }
        return self.ground_truth_source.latest_event_fields(reference_time=reference_time)

    def status(self) -> dict[str, Any]:
        latest = self.history[-1].to_dict() if self.history else None
        return {
            "model_run_id": self.state.run_id,
            "model_run_dir": str(self.state.run_dir) if self.state.run_dir else None,
            "model_loaded_at": self.state.loaded_at,
            "lookback": self.state.lookback,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "history_size": len(self.history),
            "subscribers": len(self._subscribers),
            "last_event": latest,
            "last_error": self._last_error,
            "data_source": self.data_source.label,
            "ground_truth_source": self.ground_truth_source.status() if self.ground_truth_source else None,
        }
