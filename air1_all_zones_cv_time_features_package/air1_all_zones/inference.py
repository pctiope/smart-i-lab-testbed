from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from air1_all_zones.dataset import apply_training_preprocessing
from air1_all_zones.feature_contract import (
    ALL_ZONE_IDS,
    FEATURE_COLUMNS,
    RAW_FEATURE_COLUMNS,
    SAMPLE_INTERVAL_SECONDS,
    TIMESTAMP_COLUMN,
    ZONE_ID_COLUMN,
    add_time_features,
)
from air1_all_zones.training import CURRENT_RUN_POINTER, DEFAULT_OUTPUT_DIR, TunableZoneOccupancyCNN


MODEL_FILENAME = "best_cnn_all_zones.pt"
SCALER_FILENAME = "scaler_stats_all_zones.json"
BEST_PARAMS_FILENAME = "best_params_all_zones.json"


def _resolve_artifact_run_dir(artifact_dir: str | Path) -> Path:
    base = Path(artifact_dir)
    pointer = base / CURRENT_RUN_POINTER
    if pointer.is_file():
        run_id = pointer.read_text(encoding="utf-8").strip()
        if not run_id:
            raise ValueError(f"{pointer} is empty; no model has been trained yet")
        run_dir = base / "runs" / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(
                f"{pointer} points to {run_id}, but {run_dir} does not exist"
            )
        return run_dir
    flat_model = base / "models" / MODEL_FILENAME
    if flat_model.is_file():
        return base
    raise FileNotFoundError(
        f"No trained model found under {base}: training creates {CURRENT_RUN_POINTER}; "
        "promotion creates production_run.txt for the web app."
    )


def _load_artifacts(artifact_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    run_dir = _resolve_artifact_run_dir(artifact_dir)
    scaler_path = run_dir / "tables" / SCALER_FILENAME
    best_params_path = run_dir / "tables" / BEST_PARAMS_FILENAME
    model_path = run_dir / "models" / MODEL_FILENAME

    scaler_stats = json.loads(scaler_path.read_text(encoding="utf-8"))
    best_params_payload = json.loads(best_params_path.read_text(encoding="utf-8"))
    try:
        checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)
    except TypeError:
        checkpoint = torch.load(model_path, map_location="cpu")
    return scaler_stats, best_params_payload, checkpoint


def require_10_second_model_contract(
    scaler_stats: dict[str, Any],
    best_params_payload: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    values = {"scaler_stats": scaler_stats.get("sample_interval_seconds")}
    if best_params_payload is not None:
        values["best_params"] = best_params_payload.get("sample_interval_seconds")
    if checkpoint is not None:
        values["checkpoint"] = checkpoint.get("sample_interval_seconds")
    missing = [name for name, value in values.items() if value is None]
    bad = {}
    for name, value in values.items():
        if value is None:
            continue
        try:
            interval = float(value)
        except (TypeError, ValueError):
            bad[name] = value
            continue
        if interval != float(SAMPLE_INTERVAL_SECONDS):
            bad[name] = value
    if missing or bad:
        raise ValueError(
            "model artifact is not a 10-second AIR-1 all-zones model; "
            f"missing_sample_interval={missing}, bad_sample_interval={bad}. "
            "Retrain and promote a model with sample_interval_seconds=10."
        )
    feature_columns = list(scaler_stats.get("feature_columns") or [])
    if ZONE_ID_COLUMN in feature_columns:
        raise ValueError(f"model artifact incorrectly includes {ZONE_ID_COLUMN!r} as a feature")


def _latest_rows_to_frame(latest_rows: pd.DataFrame | list[dict[str, Any]] | dict[str, Any]) -> pd.DataFrame:
    if isinstance(latest_rows, pd.DataFrame):
        return latest_rows.copy()
    if isinstance(latest_rows, dict):
        values = latest_rows.values()
        has_vector_value = any(
            isinstance(value, (list, tuple, pd.Series, np.ndarray))
            for value in values
        )
        if has_vector_value:
            return pd.DataFrame(latest_rows)
        return pd.DataFrame([latest_rows])
    return pd.DataFrame(latest_rows)


def _prepare_recent_zone_frame(
    latest_rows: pd.DataFrame | list[dict[str, Any]] | dict[str, Any],
    *,
    scaler_stats: dict[str, Any],
    checkpoint: dict[str, Any],
    zone_id: int,
    lookback: int,
    reference_time: datetime | pd.Timestamp | None,
    max_gap_minutes: float,
    max_age_minutes: float | None,
    expected_cadence_seconds: float,
) -> pd.DataFrame:
    feature_columns = list(scaler_stats["feature_columns"])
    frame = _latest_rows_to_frame(latest_rows)
    missing_required = [col for col in [TIMESTAMP_COLUMN, ZONE_ID_COLUMN] if col not in frame.columns]
    if missing_required:
        raise ValueError(f"latest rows must include required columns for live inference: {missing_required}")

    frame = frame.copy()
    frame[TIMESTAMP_COLUMN] = pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce").dt.floor(
        f"{SAMPLE_INTERVAL_SECONDS}s"
    )
    frame[ZONE_ID_COLUMN] = pd.to_numeric(frame[ZONE_ID_COLUMN], errors="coerce").astype("Int64")
    frame = frame.dropna(subset=[TIMESTAMP_COLUMN, ZONE_ID_COLUMN])
    frame = frame.loc[frame[ZONE_ID_COLUMN].astype(int) == int(zone_id)].copy()
    frame = frame.sort_values(TIMESTAMP_COLUMN).drop_duplicates(TIMESTAMP_COLUMN, keep="last")
    frame = add_time_features(frame)

    fill_values = dict(
        scaler_stats.get("feature_fill_values")
        or checkpoint.get("feature_fill_values")
        or {col: 0.0 for col in RAW_FEATURE_COLUMNS}
    )
    frame = apply_training_preprocessing(frame, fill_values)
    missing_features = [col for col in feature_columns if col not in frame.columns]
    if missing_features:
        raise ValueError(f"zone {zone_id} cannot produce required feature columns: {missing_features}")
    for col in feature_columns:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=[TIMESTAMP_COLUMN, *feature_columns])
    if len(frame) < lookback:
        raise ValueError(f"Need at least {lookback} valid recent rows for zone {zone_id}; received {len(frame)}.")

    recent = frame.tail(lookback).copy()
    deltas = recent[TIMESTAMP_COLUMN].diff().dropna()
    if not deltas.empty:
        max_gap_seen = deltas.max().total_seconds() / 60.0
        if max_gap_seen > max_gap_minutes:
            raise ValueError(
                f"Zone {zone_id} maximum timestamp gap is {max_gap_seen:.2f} min; "
                f"exceeds max_gap_minutes={max_gap_minutes}."
            )
        if expected_cadence_seconds > 0:
            min_gap_seen = deltas.min().total_seconds()
            if min_gap_seen < expected_cadence_seconds * 0.5:
                raise ValueError(
                    f"Zone {zone_id} minimum timestamp delta is {min_gap_seen:.1f} s; "
                    f"below half of expected_cadence_seconds={expected_cadence_seconds}."
                )

    if max_age_minutes is not None:
        latest_ts = recent[TIMESTAMP_COLUMN].iloc[-1]
        ref_ts = pd.Timestamp.now(tz=latest_ts.tz) if reference_time is None else pd.Timestamp(reference_time)
        if (latest_ts.tz is None) != (ref_ts.tz is None):
            raise ValueError(
                "reference_time and timestamp column must agree on timezone-awareness "
                f"(latest_ts.tz={latest_ts.tz}, reference_time.tz={ref_ts.tz})"
            )
        age_minutes = (ref_ts - latest_ts).total_seconds() / 60.0
        if age_minutes > max_age_minutes:
            raise ValueError(
                f"Zone {zone_id} latest timestamp {latest_ts} is {age_minutes:.2f} min before reference {ref_ts}; "
                f"exceeds max_age_minutes={max_age_minutes}."
            )

    for col in feature_columns:
        recent[col] = (recent[col] - float(scaler_stats["means"][col])) / float(scaler_stats["stds"][col])
    return recent


def _predict_from_recent(
    recent: pd.DataFrame,
    *,
    scaler_stats: dict[str, Any],
    best_params_payload: dict[str, Any],
    checkpoint: dict[str, Any],
) -> float:
    feature_columns = list(scaler_stats["feature_columns"])
    X = recent[feature_columns].to_numpy(dtype=np.float32).T[np.newaxis, :, :]
    params = dict(checkpoint.get("params") or best_params_payload["params"])
    model = TunableZoneOccupancyCNN(params=params, input_channels=len(feature_columns))
    state_dict = checkpoint.get("model_state_dict", checkpoint)
    model.load_state_dict(state_dict)
    model.eval()
    with torch.inference_mode():
        logits = model(torch.from_numpy(X))
        probability = float(torch.sigmoid(logits).item())
    return min(1.0, max(0.0, probability))


def predict_zone_probability(
    latest_rows: pd.DataFrame | list[dict[str, Any]] | dict[str, Any],
    zone_id: int,
    artifact_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    reference_time: datetime | pd.Timestamp | None = None,
    max_gap_minutes: float = 5.0,
    max_age_minutes: float | None = 15.0,
    expected_cadence_seconds: float = float(SAMPLE_INTERVAL_SECONDS),
) -> float:
    scaler_stats, best_params_payload, checkpoint = _load_artifacts(artifact_dir)
    require_10_second_model_contract(scaler_stats, best_params_payload, checkpoint)
    lookback = int(scaler_stats.get("lookback_rows") or scaler_stats["lookback"])
    recent = _prepare_recent_zone_frame(
        latest_rows,
        scaler_stats=scaler_stats,
        checkpoint=checkpoint,
        zone_id=zone_id,
        lookback=lookback,
        reference_time=reference_time,
        max_gap_minutes=max_gap_minutes,
        max_age_minutes=max_age_minutes,
        expected_cadence_seconds=expected_cadence_seconds,
    )
    return _predict_from_recent(recent, scaler_stats=scaler_stats, best_params_payload=best_params_payload, checkpoint=checkpoint)


def predict_all_zone_probabilities(
    latest_rows: pd.DataFrame | list[dict[str, Any]] | dict[str, Any],
    artifact_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    reference_time: datetime | pd.Timestamp | None = None,
    max_gap_minutes: float = 5.0,
    max_age_minutes: float | None = 15.0,
    expected_cadence_seconds: float = float(SAMPLE_INTERVAL_SECONDS),
    zone_ids: tuple[int, ...] = ALL_ZONE_IDS,
) -> dict[int, float]:
    scaler_stats, best_params_payload, checkpoint = _load_artifacts(artifact_dir)
    require_10_second_model_contract(scaler_stats, best_params_payload, checkpoint)
    lookback = int(scaler_stats.get("lookback_rows") or scaler_stats["lookback"])
    probabilities: dict[int, float] = {}
    for zone_id in zone_ids:
        recent = _prepare_recent_zone_frame(
            latest_rows,
            scaler_stats=scaler_stats,
            checkpoint=checkpoint,
            zone_id=int(zone_id),
            lookback=lookback,
            reference_time=reference_time,
            max_gap_minutes=max_gap_minutes,
            max_age_minutes=max_age_minutes,
            expected_cadence_seconds=expected_cadence_seconds,
        )
        probabilities[int(zone_id)] = _predict_from_recent(
            recent,
            scaler_stats=scaler_stats,
            best_params_payload=best_params_payload,
            checkpoint=checkpoint,
        )
    return probabilities
