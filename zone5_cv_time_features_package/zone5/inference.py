from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from zone5.dataset import apply_training_preprocessing, live_feature_quality_diagnostics
from zone5.feature_contract import (
    MISSING_INDICATOR_COLUMNS,
    RAW_FEATURE_COLUMNS,
    SAMPLE_INTERVAL_SECONDS,
    SUPPORTED_MODEL_FEATURE_COLUMNS_BY_CONTRACT,
    TIMESTAMP_COLUMN,
    add_time_features,
)
from zone5.training import CURRENT_RUN_POINTER, DEFAULT_OUTPUT_DIR, TunableZoneOccupancyCNN


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
    flat_model = base / "models" / "best_cnn_zone_5.pt"
    if flat_model.is_file():
        return base
    raise FileNotFoundError(
        f"No trained model found under {base}: training creates {CURRENT_RUN_POINTER}; "
        "promotion creates production_run.txt for the web app."
    )


def _load_artifacts(artifact_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    run_dir = _resolve_artifact_run_dir(artifact_dir)
    scaler_path = run_dir / "tables" / "scaler_stats_zone_5.json"
    best_params_path = run_dir / "tables" / "best_params_zone_5.json"
    model_path = run_dir / "models" / "best_cnn_zone_5.pt"

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
            "model artifact is not a 10-second Zone 5 model; "
            f"missing_sample_interval={missing}, bad_sample_interval={bad}. "
            "Retrain and promote a model with sample_interval_seconds=10."
        )

    feature_columns = list(scaler_stats.get("feature_columns") or [])
    legacy_missingness_channels = [
        col
        for col in feature_columns
        if col in MISSING_INDICATOR_COLUMNS or str(col).endswith("_missing")
    ]
    if legacy_missingness_channels:
        raise ValueError(
            "model artifact uses legacy missingness feature channels; "
            f"legacy_missingness_channels={legacy_missingness_channels}. "
            "Retrain and promote a missingness-decoupled Zone 5 model."
        )
    contract_values = {"scaler_stats": scaler_stats.get("model_contract_version")}
    if best_params_payload is not None:
        contract_values["best_params"] = best_params_payload.get("model_contract_version")
    if checkpoint is not None:
        contract_values["checkpoint"] = checkpoint.get("model_contract_version")
    missing_contract = [name for name, value in contract_values.items() if value is None]
    non_missing_contracts = {str(value) for value in contract_values.values() if value is not None}
    if missing_contract or len(non_missing_contracts) != 1:
        raise ValueError(
            "model artifact is not a supported Zone 5 model; "
            f"supported_model_contract_versions={list(SUPPORTED_MODEL_FEATURE_COLUMNS_BY_CONTRACT)}, "
            f"missing_model_contract_version={missing_contract}, "
            f"model_contract_versions={contract_values}. "
            "Retrain and promote a current model before live inference."
        )
    artifact_contract = next(iter(non_missing_contracts))
    expected_feature_columns = SUPPORTED_MODEL_FEATURE_COLUMNS_BY_CONTRACT.get(artifact_contract)
    if expected_feature_columns is None:
        raise ValueError(
            "model artifact is not a supported Zone 5 model; "
            f"supported_model_contract_versions={list(SUPPORTED_MODEL_FEATURE_COLUMNS_BY_CONTRACT)}, "
            f"bad_model_contract_version={artifact_contract!r}. "
            "Retrain and promote a current model before live inference."
        )
    if feature_columns != expected_feature_columns:
        raise ValueError(
            "model artifact feature contract does not match the current Zone 5 contract; "
            f"expected_feature_columns={expected_feature_columns}, artifact_feature_columns={feature_columns}, "
            f"artifact_model_contract_version={artifact_contract!r}. "
            "Retrain and promote a supported Zone 5 model."
        )


def _latest_minutes_to_frame(
    latest_zone_5_minutes: pd.DataFrame | list[dict[str, Any]] | dict[str, Any],
) -> pd.DataFrame:
    if isinstance(latest_zone_5_minutes, pd.DataFrame):
        return latest_zone_5_minutes.copy()
    if isinstance(latest_zone_5_minutes, dict):
        values = latest_zone_5_minutes.values()
        has_vector_value = any(
            isinstance(value, (list, tuple, pd.Series, np.ndarray))
            for value in values
        )
        if has_vector_value:
            return pd.DataFrame(latest_zone_5_minutes)
        return pd.DataFrame([latest_zone_5_minutes])
    return pd.DataFrame(latest_zone_5_minutes)


def predict_zone_5_probability(
    latest_zone_5_minutes: pd.DataFrame | list[dict[str, Any]] | dict[str, Any],
    artifact_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    reference_time: datetime | pd.Timestamp | None = None,
    max_gap_minutes: float = 5.0,
    max_age_minutes: float | None = 15.0,
    expected_cadence_seconds: float = float(SAMPLE_INTERVAL_SECONDS),
    return_diagnostics: bool = False,
) -> float | tuple[float, dict[str, Any]]:
    scaler_stats, best_params_payload, checkpoint = _load_artifacts(artifact_dir)
    require_10_second_model_contract(scaler_stats, best_params_payload, checkpoint)
    feature_columns = list(scaler_stats["feature_columns"])
    lookback = int(scaler_stats.get("lookback_rows") or scaler_stats["lookback"])

    frame = _latest_minutes_to_frame(latest_zone_5_minutes)
    if TIMESTAMP_COLUMN not in frame.columns:
        raise ValueError(
            f"latest_zone_5_minutes must include a {TIMESTAMP_COLUMN!r} column for live inference"
        )
    frame = frame.copy()
    frame[TIMESTAMP_COLUMN] = pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce").dt.floor(
        f"{SAMPLE_INTERVAL_SECONDS}s"
    )
    frame = frame.dropna(subset=[TIMESTAMP_COLUMN])
    frame = frame.sort_values(TIMESTAMP_COLUMN).drop_duplicates(TIMESTAMP_COLUMN, keep="last")
    frame = add_time_features(frame)

    if len(frame) < lookback:
        raise ValueError(f"Need at least {lookback} valid recent zone 5 rows; received {len(frame)}.")

    raw_recent = frame.tail(lookback).copy()
    diagnostics = live_feature_quality_diagnostics(raw_recent, lookback)
    if diagnostics["core_failures"]:
        detail = ", ".join(
            f"{failure['feature']} present={failure['present_fraction']:.2f} "
            f"required={failure['required_fraction']:.2f}"
            for failure in diagnostics["core_failures"]
        )
        raise ValueError(f"LIVE DATA DEGRADED: core sensor coverage below gate ({detail})")

    fill_values = dict(
        scaler_stats.get("feature_fill_values")
        or checkpoint.get("feature_fill_values")
        or {col: 0.0 for col in RAW_FEATURE_COLUMNS}
    )
    frame = apply_training_preprocessing(frame, fill_values)
    missing = [col for col in feature_columns if col not in frame.columns]
    if missing:
        raise ValueError(f"latest_zone_5_minutes cannot produce required feature columns: {missing}")
    for col in feature_columns:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=[TIMESTAMP_COLUMN, *feature_columns])
    if len(frame) < lookback:
        raise ValueError(f"Need at least {lookback} valid recent zone 5 rows; received {len(frame)}.")

    recent = frame.tail(lookback).copy()

    deltas = recent[TIMESTAMP_COLUMN].diff().dropna()
    if not deltas.empty:
        max_gap_seen = deltas.max().total_seconds() / 60.0
        if max_gap_seen > max_gap_minutes:
            raise ValueError(
                f"Maximum timestamp gap in lookback window is {max_gap_seen:.2f} min; "
                f"exceeds max_gap_minutes={max_gap_minutes}. "
                f"Window spans {recent[TIMESTAMP_COLUMN].iloc[0]} to {recent[TIMESTAMP_COLUMN].iloc[-1]}."
            )
        if expected_cadence_seconds > 0:
            min_gap_seen = deltas.min().total_seconds()
            if min_gap_seen < expected_cadence_seconds * 0.5:
                raise ValueError(
                    f"Minimum timestamp delta in lookback window is {min_gap_seen:.1f} s; "
                    f"below half of expected_cadence_seconds={expected_cadence_seconds}. "
                    "Inputs must be at 10-second cadence after deduplication."
                )

    if max_age_minutes is not None:
        latest_ts = recent[TIMESTAMP_COLUMN].iloc[-1]
        if reference_time is None:
            ref_ts = pd.Timestamp.now(tz=latest_ts.tz)
        else:
            ref_ts = pd.Timestamp(reference_time)
        if (latest_ts.tz is None) != (ref_ts.tz is None):
            raise ValueError(
                "reference_time and the timestamp column must agree on timezone-awareness "
                f"(latest_ts.tz={latest_ts.tz}, reference_time.tz={ref_ts.tz})"
            )
        age_minutes = (ref_ts - latest_ts).total_seconds() / 60.0
        if age_minutes > max_age_minutes:
            raise ValueError(
                f"Latest timestamp {latest_ts} is {age_minutes:.2f} min before reference {ref_ts}; "
                f"exceeds max_age_minutes={max_age_minutes}. "
                "Pass reference_time=<simulated_now> for replay scenarios or max_age_minutes=None to disable."
            )

    for col in feature_columns:
        recent[col] = (recent[col] - float(scaler_stats["means"][col])) / float(scaler_stats["stds"][col])

    X = recent[feature_columns].to_numpy(dtype=np.float32).T[np.newaxis, :, :]
    params = dict(checkpoint.get("params") or best_params_payload["params"])
    model = TunableZoneOccupancyCNN(params=params, input_channels=len(feature_columns))
    state_dict = checkpoint.get("model_state_dict", checkpoint)
    model.load_state_dict(state_dict)
    model.eval()

    with torch.inference_mode():
        logits = model(torch.from_numpy(X))
        probability = float(torch.sigmoid(logits).item())
    probability = min(1.0, max(0.0, probability))
    if return_diagnostics:
        return probability, diagnostics
    return probability
