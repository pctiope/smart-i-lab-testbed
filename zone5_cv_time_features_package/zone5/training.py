from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import optuna
import pandas as pd
import torch
from sklearn.metrics import log_loss
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from zone5.dataset import (
    DEFAULT_TRAINING_CSV_DIR,
    DEFAULT_TRAINING_PARQUET_DIR,
    LOOKBACK_CHOICES,
    STRICT_DATE_MIN_COVERAGE,
    _latest_training_csv,
    _latest_training_parquet,
    _frame_time_bounds,
    _window_labels_for_lookback,
    _rows_per_full_day,
    _strict_date_coverage,
    apply_scaler,
    apply_training_preprocessing,
    blind_test_split,
    blind_test_and_validation_splits,
    blind_test_evidence_by_lookback,
    build_split_windows,
    bootstrap_chronological_validation_fold,
    fill_values_from_train,
    label_evidence_counts,
    load_zone_5_csv,
    load_zone_5_parquet,
    load_zone_5_training_data,
    prepare_and_standardize_splits,
    safe_pr_auc,
    scaler_from_train,
    split_metrics,
    validation_folds_for_blind_split,
)
from zone5.feature_contract import (
    CORE_FEATURE_MIN_PRESENT_FRACTIONS,
    FEATURE_COLUMNS,
    INPUT_CHANNEL_COUNT,
    LOOKBACK_ROWS_BY_MINUTES,
    MISSING_INDICATOR_COLUMNS,
    MODEL_CONTRACT_VERSION,
    PACKAGE_ROOT,
    RAW_FEATURE_COLUMNS,
    SAMPLE_INTERVAL_SECONDS,
    SEN55_FEATURE_COLUMNS,
    TARGET_COLUMN,
    TIME_FEATURE_COLUMNS,
    TIMESTAMP_COLUMN,
    ZONE_NUM,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "model"
CURRENT_RUN_POINTER = "current_run.txt"
RUN_MANIFEST_FILENAME = "manifest.json"
STRICT_VALIDATION_MODE = "rolling_calendar"
BOOTSTRAP_VALIDATION_MODE = "bootstrap_chronological_fallback"
STRICT_NO_VIABLE_LOOKBACKS_REASON = "strict_validation_no_viable_lookback_candidates"
MIN_STRICT_CV_FOLDS = 1
MAX_STRICT_CV_FOLDS = 3

BATCH_SIZE_CHOICES = [64, 128, 256]
BASE_CHANNELS_CHOICES = [32, 64]
DENSE_UNITS_CHOICES = [32, 64, 128]
LR_SCHEDULER_CHOICES = ["cosine", "reduce_on_plateau"]
DEFAULT_N_TRIALS = 50
DEFAULT_MAX_EPOCHS = 20
DEFAULT_SEED = 42
GRAD_CLIP_MAX_NORM = 1.0
DEFAULT_SEN55_DROPOUT_PROBABILITY = 0.20
SEN55_FEATURE_CHANNELS = [FEATURE_COLUMNS.index(col) for col in SEN55_FEATURE_COLUMNS if col in FEATURE_COLUMNS]


def validate_cv_folds(value: int | str) -> int:
    folds = int(value)
    if folds < MIN_STRICT_CV_FOLDS or folds > MAX_STRICT_CV_FOLDS:
        raise ValueError(
            f"cv_folds must be between {MIN_STRICT_CV_FOLDS} and {MAX_STRICT_CV_FOLDS}; got {value}."
        )
    return folds


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(data), indent=2, sort_keys=True), encoding="utf-8")


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PACKAGE_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _detect_git_rev(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    rev = result.stdout.strip()
    return rev or None


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _default_optuna_jobs(device: torch.device) -> int:
    if device.type == "cuda":
        return 1
    cpu_count = os.cpu_count() or 1
    return min(4, max(1, cpu_count // 2))


def _lookback_minutes_for_rows(lookback_rows: int) -> int | float:
    for minutes, rows in LOOKBACK_ROWS_BY_MINUTES.items():
        if int(rows) == int(lookback_rows):
            return int(minutes)
    return float(lookback_rows) * SAMPLE_INTERVAL_SECONDS / 60.0

def make_activation(name: str) -> nn.Module:
    if name == "ReLU":
        return nn.ReLU()
    if name == "GELU":
        return nn.GELU()
    if name == "SiLU":
        return nn.SiLU()
    raise ValueError(f"Unsupported activation: {name}")


def should_pool(block_idx: int, pooling_pattern: str) -> bool:
    if block_idx < 1:
        return False
    if pooling_pattern == "after_block_2":
        return block_idx == 1
    if pooling_pattern == "after_every_late_block":
        return True
    raise ValueError(f"Unsupported pooling pattern: {pooling_pattern}")


class TunableZoneOccupancyCNN(nn.Module):
    def __init__(self, params: dict[str, Any], input_channels: int = INPUT_CHANNEL_COUNT):
        super().__init__()
        features: list[nn.Module] = []
        in_channels = input_channels

        for block_idx in range(int(params["num_conv_blocks"])):
            out_channels = min(int(params["base_channels"]) * (2**block_idx), 256)
            kernel_size = int(params["kernel_size"] if block_idx == 0 else params["late_kernel_size"])
            features.append(
                nn.Conv1d(
                    in_channels,
                    out_channels,
                    kernel_size=kernel_size,
                    padding=kernel_size // 2,
                )
            )
            if params["normalization"] == "batchnorm":
                features.append(nn.BatchNorm1d(out_channels))
            features.append(make_activation(str(params["activation"])))
            if should_pool(block_idx, str(params["pooling_pattern"])):
                features.append(nn.MaxPool1d(2))
            in_channels = out_channels

        self.features = nn.Sequential(*features)
        if params["global_pool"] == "avg":
            self.global_pool = nn.AdaptiveAvgPool1d(1)
        else:
            self.global_pool = nn.AdaptiveMaxPool1d(1)

        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(in_channels, int(params["dense_units"])),
            make_activation(str(params["activation"])),
            nn.Dropout(float(params["dropout"])),
            nn.Linear(int(params["dense_units"]), 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.features(x)
        x = self.global_pool(x)
        x = self.classifier(x)
        return x.squeeze(-1)


def make_loader(X: np.ndarray, y: np.ndarray, batch_size: int, shuffle: bool) -> DataLoader:
    dataset = TensorDataset(torch.from_numpy(X), torch.from_numpy(y))
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, num_workers=0)


def _apply_sen55_dropout(
    xb: torch.Tensor,
    sen55_neutral_values: list[float] | tuple[float, ...] | None,
    probability: float = DEFAULT_SEN55_DROPOUT_PROBABILITY,
) -> torch.Tensor:
    if not SEN55_FEATURE_CHANNELS or probability <= 0.0 or xb.numel() == 0:
        return xb
    if not sen55_neutral_values or len(sen55_neutral_values) != len(SEN55_FEATURE_CHANNELS):
        neutral_values = [0.0 for _ in SEN55_FEATURE_CHANNELS]
    else:
        neutral_values = [float(value) for value in sen55_neutral_values]
    drop_mask = torch.rand((xb.shape[0], 1, 1), device=xb.device) < float(probability)
    if not bool(drop_mask.any()):
        return xb
    augmented = xb.clone()
    neutral = torch.tensor(neutral_values, dtype=xb.dtype, device=xb.device).view(1, -1, 1)
    replacement = neutral.expand(xb.shape[0], len(SEN55_FEATURE_CHANNELS), xb.shape[2])
    augmented[:, SEN55_FEATURE_CHANNELS, :] = torch.where(
        drop_mask,
        replacement,
        augmented[:, SEN55_FEATURE_CHANNELS, :],
    )
    return augmented


def build_optimizer(params: dict[str, Any], model: nn.Module) -> torch.optim.Optimizer:
    return torch.optim.AdamW(
        model.parameters(),
        lr=float(params["learning_rate"]),
        weight_decay=float(params["weight_decay"]),
    )


def build_scheduler(
    params: dict[str, Any],
    optimizer: torch.optim.Optimizer,
    max_epochs: int,
) -> torch.optim.lr_scheduler.LRScheduler | torch.optim.lr_scheduler.ReduceLROnPlateau | None:
    scheduler_name = str(params.get("lr_scheduler", "none"))
    if scheduler_name == "cosine":
        return torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, max_epochs))
    if scheduler_name == "reduce_on_plateau":
        return torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="max", patience=3, factor=0.5)
    return None


def suggest_hyperparameters(trial: optuna.trial.Trial, lookback_candidates: list[int]) -> dict[str, Any]:
    return {
        "lookback": int(trial.suggest_categorical("lookback", lookback_candidates)),
        "batch_size": int(trial.suggest_categorical("batch_size", BATCH_SIZE_CHOICES)),
        "optimizer": "AdamW",
        "learning_rate": float(trial.suggest_float("learning_rate", 1e-4, 5e-3, log=True)),
        "weight_decay": float(trial.suggest_float("weight_decay", 1e-6, 1e-2, log=True)),
        "num_conv_blocks": int(trial.suggest_categorical("num_conv_blocks", [2, 3])),
        "base_channels": int(trial.suggest_categorical("base_channels", BASE_CHANNELS_CHOICES)),
        "kernel_size": int(trial.suggest_categorical("kernel_size", [3, 5, 7])),
        "late_kernel_size": int(trial.suggest_categorical("late_kernel_size", [3, 5, 7])),
        "activation": str(trial.suggest_categorical("activation", ["ReLU", "GELU", "SiLU"])),
        "normalization": str(trial.suggest_categorical("normalization", ["none", "batchnorm"])),
        "pooling_pattern": str(
            trial.suggest_categorical("pooling_pattern", ["after_block_2", "after_every_late_block"])
        ),
        "global_pool": str(trial.suggest_categorical("global_pool", ["avg", "max"])),
        "dense_units": int(trial.suggest_categorical("dense_units", DENSE_UNITS_CHOICES)),
        "dropout": float(trial.suggest_float("dropout", 0.0, 0.3)),
        "lr_scheduler": str(trial.suggest_categorical("lr_scheduler", LR_SCHEDULER_CHOICES)),
        "patience": int(trial.suggest_int("patience", 3, 7)),
    }


def predict_scores(
    model: nn.Module,
    X: np.ndarray,
    batch_size: int,
    device: torch.device,
) -> np.ndarray:
    dummy_y = np.zeros((X.shape[0],), dtype=np.float32)
    loader = make_loader(X, dummy_y, batch_size=batch_size, shuffle=False)
    model.eval()
    probs: list[np.ndarray] = []
    with torch.inference_mode():
        for xb, _ in loader:
            xb = xb.to(device)
            logits = model(xb)
            probs.append(torch.sigmoid(logits).detach().cpu().numpy())
    if not probs:
        return np.asarray([], dtype=np.float32)
    return np.concatenate(probs).astype(np.float32)


def train_model(
    split_windows: dict[str, dict[str, Any]],
    params: dict[str, Any],
    pos_weight_value: float,
    max_epochs: int,
    device: torch.device,
    trial: optuna.trial.Trial | None = None,
    sen55_neutral_values: list[float] | tuple[float, ...] | None = None,
    sen55_dropout_probability: float = DEFAULT_SEN55_DROPOUT_PROBABILITY,
) -> tuple[nn.Module, dict[str, list[float | int]], float]:
    train_loader = make_loader(
        split_windows["train"]["X"],
        split_windows["train"]["y"],
        batch_size=int(params["batch_size"]),
        shuffle=True,
    )
    model = TunableZoneOccupancyCNN(params).to(device)
    criterion = nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor([pos_weight_value], dtype=torch.float32, device=device)
    )
    monitor_criterion = nn.BCEWithLogitsLoss()
    optimizer = build_optimizer(params, model)
    scheduler = build_scheduler(params, optimizer, max_epochs=max_epochs)

    history: dict[str, list[float | int]] = {"epoch": [], "train_loss": [], "val_loss": [], "val_pr_auc": []}
    best_state = deepcopy(model.state_dict())
    best_pr_auc = -math.inf
    best_epoch = 0
    patience_counter = 0
    patience = int(params.get("patience", 3))

    X_val = split_windows["val"]["X"]
    y_val = split_windows["val"]["y"]

    for epoch in range(1, max_epochs + 1):
        model.train()
        train_losses: list[float] = []
        for xb, yb in train_loader:
            xb = xb.to(device)
            yb = yb.to(device).float()
            optimizer.zero_grad(set_to_none=True)
            xb = _apply_sen55_dropout(
                xb,
                sen55_neutral_values,
                probability=sen55_dropout_probability,
            )
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=GRAD_CLIP_MAX_NORM)
            optimizer.step()
            with torch.no_grad():
                train_losses.append(float(monitor_criterion(logits.detach(), yb).cpu()))

        val_scores = predict_scores(model, X_val, int(params["batch_size"]), device)
        val_loss = float(log_loss(y_val.astype(int), np.clip(val_scores, 1e-7, 1.0 - 1e-7), labels=[0, 1]))
        val_pr_auc = safe_pr_auc(y_val, val_scores)
        report_score = val_pr_auc if math.isfinite(val_pr_auc) else -1.0

        history["epoch"].append(epoch)
        history["train_loss"].append(float(np.mean(train_losses)) if train_losses else float("nan"))
        history["val_loss"].append(val_loss)
        history["val_pr_auc"].append(val_pr_auc)

        if scheduler is not None:
            if isinstance(scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                scheduler.step(report_score)
            else:
                scheduler.step()

        if report_score > best_pr_auc:
            best_pr_auc = report_score
            best_state = deepcopy(model.state_dict())
            best_epoch = epoch
            patience_counter = 0
        else:
            patience_counter += 1

        if trial is not None:
            trial.report(report_score, step=epoch)

        if patience_counter >= patience:
            break

    model.load_state_dict(best_state)
    history["best_epoch"] = [best_epoch for _ in history["epoch"]]
    return model, history, float(best_pr_auc)


def train_model_fixed_epochs(
    train_windows: dict[str, Any],
    params: dict[str, Any],
    pos_weight_value: float,
    epochs: int,
    device: torch.device,
    sen55_neutral_values: list[float] | tuple[float, ...] | None = None,
    sen55_dropout_probability: float = DEFAULT_SEN55_DROPOUT_PROBABILITY,
) -> tuple[nn.Module, dict[str, list[float | int]]]:
    train_loader = make_loader(
        train_windows["X"],
        train_windows["y"],
        batch_size=int(params["batch_size"]),
        shuffle=True,
    )
    model = TunableZoneOccupancyCNN(params).to(device)
    criterion = nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor([pos_weight_value], dtype=torch.float32, device=device)
    )
    monitor_criterion = nn.BCEWithLogitsLoss()
    optimizer = build_optimizer(params, model)
    scheduler = build_scheduler(params, optimizer, max_epochs=max(1, int(epochs)))
    history: dict[str, list[float | int]] = {"epoch": [], "train_loss": []}

    for epoch in range(1, max(1, int(epochs)) + 1):
        model.train()
        train_losses: list[float] = []
        for xb, yb in train_loader:
            xb = xb.to(device)
            yb = yb.to(device).float()
            optimizer.zero_grad(set_to_none=True)
            xb = _apply_sen55_dropout(
                xb,
                sen55_neutral_values,
                probability=sen55_dropout_probability,
            )
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=GRAD_CLIP_MAX_NORM)
            optimizer.step()
            with torch.no_grad():
                train_losses.append(float(monitor_criterion(logits.detach(), yb).cpu()))
        if scheduler is not None and not isinstance(scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
            scheduler.step()
        history["epoch"].append(epoch)
        history["train_loss"].append(float(np.mean(train_losses)) if train_losses else float("nan"))
    return model, history


def _final_training_epochs_from_cv(
    fold_metrics: list[dict[str, Any]],
    max_epochs: int,
) -> int:
    epochs = [
        int(metric.get("best_epoch", 0))
        for metric in fold_metrics
        if int(metric.get("best_epoch", 0) or 0) > 0
    ]
    if not epochs:
        return max(1, int(max_epochs))
    return min(max(1, int(max_epochs)), max(1, int(math.ceil(float(np.median(epochs))))))


def _build_objective(
    scaled_splits: dict[str, pd.DataFrame],
    lookback_candidates: list[int],
    max_epochs: int,
    device: torch.device,
) -> Any:
    window_cache: dict[int, dict[str, dict[str, Any]]] = {}

    def objective(trial: optuna.trial.Trial) -> float:
        params = suggest_hyperparameters(trial, lookback_candidates)
        lookback = int(params["lookback"])
        if lookback not in window_cache:
            window_cache[lookback] = build_split_windows(scaled_splits, lookback)
        split_windows = window_cache[lookback]
        y_train = split_windows["train"]["y"]
        positives = float(y_train.sum())
        negatives = float(len(y_train) - positives)
        pos_weight = negatives / max(positives, 1.0)

        trial.set_user_attr("train_positive_rate", float(y_train.mean()) if y_train.size else float("nan"))
        trial.set_user_attr(
            "val_positive_rate",
            float(split_windows["val"]["y"].mean()) if split_windows["val"]["y"].size else float("nan"),
        )
        trial.set_user_attr("pos_weight", float(pos_weight))

        _, history, best_pr_auc = train_model(
            split_windows=split_windows,
            params=params,
            pos_weight_value=pos_weight,
            max_epochs=max_epochs,
            device=device,
            trial=trial,
            sen55_neutral_values=split_windows["train"].get("sen55_neutral_values"),
        )
        trial.set_user_attr("best_epoch", int(history["best_epoch"][0]) if history["best_epoch"] else 0)
        trial.set_user_attr("epochs_ran", len(history["epoch"]))
        if device.type == "cuda":
            torch.cuda.empty_cache()
        return float(best_pr_auc)

    return objective


def viable_cv_lookback_candidates(
    cv_folds: list[dict[str, Any]],
    final_splits: dict[str, pd.DataFrame],
    allow_degenerate_validation: bool = False,
) -> tuple[list[int], dict[int, list[str]]]:
    min_rows = min(
        [len(final_splits["pre_test"]), len(final_splits["test"])]
        + [len(fold["train"]) for fold in cv_folds]
        + [len(fold["val"]) for fold in cv_folds]
    )
    candidates = [lookback for lookback in LOOKBACK_CHOICES if lookback <= min_rows]
    rejected: dict[int, list[str]] = {}
    viable: list[int] = []

    for lookback in candidates:
        reasons: list[str] = []
        final_train_y = _window_labels_for_lookback(final_splits["pre_test"], lookback)
        if np.unique(final_train_y.astype(int)).size < 2:
            reasons.append("final_pre_test_windows_single_class")
        for fold in cv_folds:
            train_y = _window_labels_for_lookback(fold["train"], lookback)
            val_y = _window_labels_for_lookback(fold["val"], lookback)
            if np.unique(train_y.astype(int)).size < 2:
                reasons.append(f"{fold['name']}_train_windows_single_class")
            if not allow_degenerate_validation and np.unique(val_y.astype(int)).size < 2:
                reasons.append(f"{fold['name']}_val_windows_single_class")
        if reasons:
            rejected[lookback] = reasons
            continue
        viable.append(lookback)

    return viable, rejected


def _prepare_cv_fold_payloads(
    cv_folds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for fold in cv_folds:
        raw_splits = {"train": fold["train"], "val": fold["val"]}
        scaled_splits, scaler_stats, fill_values = prepare_and_standardize_splits(raw_splits)
        payloads.append(
            {
                "name": fold["name"],
                "scaled_splits": scaled_splits,
                "scaler_stats": scaler_stats,
                "feature_fill_values": fill_values,
                "validation_mode": fold.get("validation_mode", "rolling_calendar"),
                "train_bounds": fold["train_bounds"],
                "validation_bounds": fold["validation_bounds"],
                "validation_start_date": fold["validation_start_date"],
                "validation_end_date": fold["validation_end_date"],
            }
        )
    return payloads


def _fold_policy_bounds(fold: dict[str, Any]) -> dict[str, Any]:
    return {
        "fold": fold["name"],
        "validation_mode": fold.get("validation_mode", "rolling_calendar"),
        "validation_start_date": fold["validation_start_date"],
        "validation_end_date": fold["validation_end_date"],
        "train_bounds": fold["train_bounds"],
        "validation_bounds": fold["validation_bounds"],
    }


def _split_lengths(blind_splits: dict[str, pd.DataFrame], cv_folds: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "pre_test": len(blind_splits["pre_test"]),
        "test": len(blind_splits["test"]),
        **{f"{fold['name']}_train": len(fold["train"]) for fold in cv_folds},
        **{f"{fold['name']}_val": len(fold["val"]) for fold in cv_folds},
    }


def select_cv_lookback_plan(
    frame: pd.DataFrame,
    *,
    cv_folds: int = MAX_STRICT_CV_FOLDS,
    bootstrap_fallback: bool = False,
    allow_degenerate_validation: bool = False,
    cv_folds_policy: dict[str, Any] | None = None,
    min_strict_date_coverage: float = STRICT_DATE_MIN_COVERAGE,
) -> dict[str, Any]:
    requested_cv_folds = validate_cv_folds(cv_folds)
    try:
        blind_splits = blind_test_split(frame)
    except ValueError as exc:
        if not bootstrap_fallback:
            raise
        timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN])
        unique_dates = sorted(timestamps.dt.normalize().unique())
        if len(unique_dates) != 1:
            raise
        bootstrap_fold = bootstrap_chronological_validation_fold(frame)
        bootstrap_lookback_candidates, bootstrap_rejected_lookbacks = viable_cv_lookback_candidates(
            [bootstrap_fold],
            {"pre_test": frame.copy().reset_index(drop=True), "test": frame.copy().reset_index(drop=True)},
            allow_degenerate_validation=allow_degenerate_validation,
        )
        bootstrap_fold_bounds = _fold_policy_bounds(bootstrap_fold)
        bootstrap_split_policy = {
            "validation_mode": "single_day_bootstrap_no_blind_test",
            "blind_test_calendar_days": 0,
            "blind_test_reused_pre_test": True,
            "bootstrap_fallback_enabled": True,
            "bootstrap_fallback_used": True,
            "bootstrap_fallback_reason": str(exc),
            "cv_folds": 1,
            "cv_folds_requested": int(requested_cv_folds),
            "cv_folds_used": 1,
            "cv_validation_calendar_days": 0,
            "strict_date_min_coverage": float(min_strict_date_coverage),
            "strict_date_full_day_rows": int(_rows_per_full_day()),
            "strict_date_eligible_dates": [date.date().isoformat() for date in unique_dates],
            "strict_date_excluded_dates": [],
            "pre_test_bounds": _frame_time_bounds(frame),
            "blind_test_bounds": _frame_time_bounds(frame),
            "cv_fold_bounds": [bootstrap_fold_bounds],
            "bootstrap_fold_bounds": bootstrap_fold_bounds,
            "strict_validation_error": str(exc),
        }
        return {
            "blind_splits": {"pre_test": frame.copy().reset_index(drop=True), "test": frame.copy().reset_index(drop=True)},
            "cv_folds": [bootstrap_fold],
            "split_policy": bootstrap_split_policy,
            "lookback_candidates": bootstrap_lookback_candidates,
            "rejected_lookbacks": bootstrap_rejected_lookbacks,
            "strict_rejected_lookbacks": {},
        }
    strict_validation_error: str | None = None
    try:
        strict_cv_folds, strict_split_policy = validation_folds_for_blind_split(
            blind_splits,
            n_folds=requested_cv_folds,
            min_strict_date_coverage=min_strict_date_coverage,
        )
        strict_lookback_candidates, strict_rejected_lookbacks = viable_cv_lookback_candidates(
            strict_cv_folds,
            blind_splits,
            allow_degenerate_validation=allow_degenerate_validation,
        )
    except ValueError as exc:
        strict_validation_error = str(exc)
        strict_cv_folds = []
        strict_eligible_dates, strict_excluded_dates = _strict_date_coverage(
            blind_splits["pre_test"],
            min_coverage=float(min_strict_date_coverage),
        )
        strict_split_policy = {
            "validation_mode": STRICT_VALIDATION_MODE,
            "blind_test_calendar_days": 1,
            "cv_folds": 0,
            "cv_folds_requested": int(requested_cv_folds),
            "cv_folds_used": 0,
            "cv_validation_calendar_days": 1,
            "strict_date_min_coverage": float(min_strict_date_coverage),
            "strict_date_full_day_rows": int(_rows_per_full_day()),
            "strict_date_eligible_dates": [date.date().isoformat() for date in strict_eligible_dates],
            "strict_date_excluded_dates": strict_excluded_dates,
            "final_model_training_rows": "all rows before the blind test split",
            "pre_test_bounds": _frame_time_bounds(blind_splits["pre_test"]),
            "blind_test_bounds": _frame_time_bounds(blind_splits["test"]),
            "cv_fold_bounds": [],
            "strict_validation_error": strict_validation_error,
        }
        strict_rejected_lookbacks = {
            int(lookback): ["strict_validation_no_eligible_cv_folds"]
            for lookback in LOOKBACK_CHOICES
        }
        strict_lookback_candidates = []
    strict_split_policy = {
        **strict_split_policy,
        "bootstrap_fallback_enabled": bool(bootstrap_fallback),
        "bootstrap_fallback_used": False,
    }
    if cv_folds_policy is not None:
        strict_split_policy["cv_folds_policy"] = cv_folds_policy
    if strict_lookback_candidates or not bootstrap_fallback:
        return {
            "blind_splits": blind_splits,
            "cv_folds": strict_cv_folds,
            "split_policy": strict_split_policy,
            "lookback_candidates": strict_lookback_candidates,
            "rejected_lookbacks": strict_rejected_lookbacks,
            "strict_rejected_lookbacks": strict_rejected_lookbacks,
        }

    bootstrap_fold = bootstrap_chronological_validation_fold(blind_splits["pre_test"])
    bootstrap_cv_folds = [bootstrap_fold]
    bootstrap_lookback_candidates, bootstrap_rejected_lookbacks = viable_cv_lookback_candidates(
        bootstrap_cv_folds,
        blind_splits,
        allow_degenerate_validation=allow_degenerate_validation,
    )
    bootstrap_fold_bounds = _fold_policy_bounds(bootstrap_fold)
    bootstrap_split_policy = {
        **strict_split_policy,
        "validation_mode": BOOTSTRAP_VALIDATION_MODE,
        "bootstrap_fallback_used": True,
        "bootstrap_fallback_reason": STRICT_NO_VIABLE_LOOKBACKS_REASON,
        "strict_validation_mode": strict_split_policy.get("validation_mode"),
        "strict_cv_folds_used": strict_split_policy.get("cv_folds_used"),
        "strict_cv_fold_bounds": strict_split_policy.get("cv_fold_bounds", []),
        "strict_rejected_lookbacks": strict_rejected_lookbacks,
        "cv_folds": 1,
        "cv_folds_used": 1,
        "cv_fold_bounds": [bootstrap_fold_bounds],
        "bootstrap_fold_bounds": bootstrap_fold_bounds,
    }
    return {
        "blind_splits": blind_splits,
        "cv_folds": bootstrap_cv_folds,
        "split_policy": bootstrap_split_policy,
        "lookback_candidates": bootstrap_lookback_candidates,
        "rejected_lookbacks": bootstrap_rejected_lookbacks,
        "strict_rejected_lookbacks": strict_rejected_lookbacks,
    }


def _build_cv_objective(
    cv_fold_payloads: list[dict[str, Any]],
    lookback_candidates: list[int],
    max_epochs: int,
    device: torch.device,
) -> Any:
    def objective(trial: optuna.trial.Trial) -> float:
        params = suggest_hyperparameters(trial, lookback_candidates)
        lookback = int(params["lookback"])
        fold_scores: list[float] = []
        fold_metrics: list[dict[str, Any]] = []

        for fold in cv_fold_payloads:
            split_windows = build_split_windows(fold["scaled_splits"], lookback)
            y_train = split_windows["train"]["y"]
            positives = float(y_train.sum())
            negatives = float(len(y_train) - positives)
            pos_weight = negatives / max(positives, 1.0)
            model, history, best_pr_auc = train_model(
                split_windows=split_windows,
                params=params,
                pos_weight_value=pos_weight,
                max_epochs=max_epochs,
                device=device,
                trial=None,
                sen55_neutral_values=split_windows["train"].get("sen55_neutral_values"),
            )
            val_scores = predict_scores(
                model,
                split_windows["val"]["X"],
                batch_size=int(params["batch_size"]),
                device=device,
            )
            val_metrics = split_metrics(split_windows["val"]["y"], val_scores)
            fold_scores.append(float(best_pr_auc))
            fold_summary = {
                "fold": fold["name"],
                "validation_mode": fold["validation_mode"],
                "validation_start_date": fold["validation_start_date"],
                "validation_end_date": fold["validation_end_date"],
                "train_bounds": fold["train_bounds"],
                "validation_bounds": fold["validation_bounds"],
                "train_windows": int(split_windows["train"]["X"].shape[0]),
                "validation_windows": int(split_windows["val"]["X"].shape[0]),
                "train_positive_rate": float(y_train.mean()) if y_train.size else float("nan"),
                "validation_positive_rate": (
                    float(split_windows["val"]["y"].mean()) if split_windows["val"]["y"].size else float("nan")
                ),
                "pos_weight": float(pos_weight),
                "best_epoch": int(history["best_epoch"][0]) if history["best_epoch"] else 0,
                "epochs_ran": len(history["epoch"]),
                "best_validation_pr_auc": float(best_pr_auc),
                "validation_metrics": val_metrics,
            }
            fold_metrics.append(fold_summary)
            trial.set_user_attr(f"{fold['name']}_best_validation_pr_auc", float(best_pr_auc))
            trial.set_user_attr(f"{fold['name']}_epochs_ran", len(history["epoch"]))
            if device.type == "cuda":
                torch.cuda.empty_cache()

        finite_scores = [score for score in fold_scores if math.isfinite(score)]
        mean_score = float(np.mean(finite_scores)) if finite_scores else -1.0
        std_score = float(np.std(finite_scores)) if finite_scores else float("nan")
        trial.set_user_attr("cv_fold_metrics", fold_metrics)
        trial.set_user_attr("mean_cv_pr_auc", mean_score)
        trial.set_user_attr("std_cv_pr_auc", std_score)
        trial.set_user_attr("cv_folds", len(cv_fold_payloads))
        return mean_score

    return objective


def evaluate_model(
    model: nn.Module,
    split_windows: dict[str, dict[str, Any]],
    batch_size: int,
    device: torch.device,
) -> tuple[pd.DataFrame, dict[str, dict[str, float | int]]]:
    prediction_frames: list[pd.DataFrame] = []
    metrics: dict[str, dict[str, float | int]] = {}

    for split_name, payload in split_windows.items():
        y_true = payload["y"]
        scores = predict_scores(model, payload["X"], batch_size=batch_size, device=device)
        timestamps = pd.to_datetime(payload["timestamps"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        prediction_frames.append(
            pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "split": split_name,
                    "y_true": y_true.astype(int),
                    "occupancy_probability": scores.astype(float),
                }
            )
        )
        metrics[split_name] = split_metrics(y_true, scores)

    return pd.concat(prediction_frames, ignore_index=True), metrics


def train_zone_5_from_csv(
    csv_path: str | Path | None = None,
    parquet_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    n_trials: int = DEFAULT_N_TRIALS,
    optuna_jobs: int | None = None,
    max_epochs: int = DEFAULT_MAX_EPOCHS,
    seed: int = DEFAULT_SEED,
    allow_bad_lines: bool = False,
    allow_degenerate_validation: bool = False,
    bootstrap_fallback: bool = False,
    cv_folds: int = MAX_STRICT_CV_FOLDS,
    cv_folds_policy: dict[str, Any] | None = None,
    min_strict_date_coverage: float = STRICT_DATE_MIN_COVERAGE,
) -> dict[str, Any]:
    _set_seed(seed)
    requested_cv_folds = validate_cv_folds(cv_folds)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if optuna_jobs is None:
        optuna_jobs = _default_optuna_jobs(device)

    output_path = Path(output_dir)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    run_dir = output_path / "runs" / run_id
    models_dir = run_dir / "models"
    tables_dir = run_dir / "tables"
    for directory in [models_dir, tables_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    frame, source_data_path, source_data_format = load_zone_5_training_data(
        csv_path=csv_path,
        parquet_path=parquet_path,
        allow_bad_lines=allow_bad_lines,
    )
    portable_source_data_path = _portable_path(source_data_path)
    cv_plan = select_cv_lookback_plan(
        frame,
        cv_folds=requested_cv_folds,
        bootstrap_fallback=bootstrap_fallback,
        allow_degenerate_validation=allow_degenerate_validation,
        cv_folds_policy=cv_folds_policy,
        min_strict_date_coverage=min_strict_date_coverage,
    )
    blind_splits = cv_plan["blind_splits"]
    cv_folds = cv_plan["cv_folds"]
    split_policy = cv_plan["split_policy"]
    lookback_candidates = cv_plan["lookback_candidates"]
    rejected_lookbacks = cv_plan["rejected_lookbacks"]
    if not lookback_candidates:
        split_lengths = _split_lengths(blind_splits, cv_folds)
        validation_mode = str(split_policy.get("validation_mode", "validation"))
        if validation_mode == BOOTSTRAP_VALIDATION_MODE:
            failure_context = "bootstrap fallback validation folds"
        else:
            failure_context = "all rolling CV folds"
        raise ValueError(
            f"No lookback candidates fit {failure_context} with usable train/validation labels. "
            f"Split lengths: {split_lengths}; rejected lookbacks: {rejected_lookbacks}. "
            f"Split policy: {split_policy}. "
            "Use a longer training file or pass allow_degenerate_validation=True / --allow-degenerate-validation "
            "only for smoke tests. For a first model only, pass bootstrap_fallback=True / --bootstrap-fallback "
            "to retry with a single chronological bootstrap validation fold."
        )
    cv_fold_payloads = _prepare_cv_fold_payloads(cv_folds)

    sampler = optuna.samplers.TPESampler(seed=seed, multivariate=True)
    study_name = f"cnn_1d_zone_5_training_csv_{uuid.uuid4().hex[:12]}"
    study = optuna.create_study(
        direction="maximize",
        sampler=sampler,
        study_name=study_name,
    )
    objective = _build_cv_objective(cv_fold_payloads, lookback_candidates, max_epochs=max_epochs, device=device)
    study.optimize(objective, n_trials=int(n_trials), n_jobs=int(optuna_jobs), show_progress_bar=False)

    best_params = dict(study.best_trial.params)
    best_params["optimizer"] = "AdamW"
    best_params["lookback"] = int(best_params["lookback"])
    best_lookback = int(best_params["lookback"])
    best_lookback_minutes = _lookback_minutes_for_rows(best_lookback)
    final_fill_values = fill_values_from_train(blind_splits["pre_test"])
    final_train_frame = apply_training_preprocessing(blind_splits["pre_test"], final_fill_values)
    final_scaler_stats = scaler_from_train(final_train_frame)
    final_train_frame = apply_scaler(final_train_frame, final_scaler_stats)
    final_train_windows = build_split_windows({"train": final_train_frame}, best_lookback)
    y_train = final_train_windows["train"]["y"]
    positives = float(y_train.sum())
    negatives = float(len(y_train) - positives)
    pos_weight = negatives / max(positives, 1.0)
    cv_fold_metrics = study.best_trial.user_attrs.get("cv_fold_metrics", [])
    final_training_epochs = _final_training_epochs_from_cv(cv_fold_metrics, max_epochs=max_epochs)

    _set_seed(seed)
    final_model, _history = train_model_fixed_epochs(
        train_windows=final_train_windows["train"],
        params=best_params,
        pos_weight_value=pos_weight,
        device=device,
        epochs=final_training_epochs,
        sen55_neutral_values=final_train_windows["train"].get("sen55_neutral_values"),
    )

    final_eval_splits = {
        "pre_test": apply_scaler(apply_training_preprocessing(blind_splits["pre_test"], final_fill_values), final_scaler_stats),
        "test": apply_scaler(apply_training_preprocessing(blind_splits["test"], final_fill_values), final_scaler_stats),
    }
    final_eval_windows = build_split_windows(final_eval_splits, best_lookback)
    _predictions_df, metrics = evaluate_model(
        final_model,
        final_eval_windows,
        batch_size=int(best_params["batch_size"]),
        device=device,
    )
    metrics["blind_test"] = metrics["test"]
    for split_name, raw_split in {"pre_test": blind_splits["pre_test"], "test": blind_splits["test"]}.items():
        metrics[split_name].update(label_evidence_counts(raw_split))
    metrics["blind_test"] = metrics["test"]

    model_path = models_dir / "best_cnn_zone_5.pt"
    checkpoint = {
        "model_state_dict": {key: value.detach().cpu() for key, value in final_model.state_dict().items()},
        "params": _json_safe(best_params),
        "model_contract_version": MODEL_CONTRACT_VERSION,
        "feature_columns": FEATURE_COLUMNS,
        "target_column": TARGET_COLUMN,
        "lookback": best_lookback,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "lookback_minutes": best_lookback_minutes,
        "lookback_rows": best_lookback,
        "input_channels": INPUT_CHANNEL_COUNT,
        "feature_fill_values": final_fill_values,
        "missing_indicator_columns": MISSING_INDICATOR_COLUMNS,
        "core_feature_min_present_fractions": CORE_FEATURE_MIN_PRESENT_FRACTIONS,
        "sen55_optional": True,
        "sen55_dropout_probability": DEFAULT_SEN55_DROPOUT_PROBABILITY,
        "source_data_path": portable_source_data_path,
        "source_data_format": source_data_format,
        "source_csv_path": portable_source_data_path if source_data_format == "csv" else None,
        "source_parquet_path": portable_source_data_path if source_data_format == "parquet" else None,
    }
    torch.save(checkpoint, model_path)

    split_sizes = {
        "rows_total": int(len(frame)),
        "pre_test_rows": int(len(blind_splits["pre_test"])),
        "test_rows": int(len(blind_splits["test"])),
        "final_train_rows": int(len(blind_splits["pre_test"])),
        "final_train_windows": int(final_train_windows["train"]["X"].shape[0]),
        "pre_test_windows": int(final_eval_windows["pre_test"]["X"].shape[0]),
        "test_windows": int(final_eval_windows["test"]["X"].shape[0]),
    }
    test_timestamps = set(pd.to_datetime(blind_splits["test"][TIMESTAMP_COLUMN]).astype("int64"))
    cv_timestamps: set[int] = set()
    for fold in cv_folds:
        cv_timestamps.update(pd.to_datetime(fold["train"][TIMESTAMP_COLUMN]).astype("int64").tolist())
        cv_timestamps.update(pd.to_datetime(fold["val"][TIMESTAMP_COLUMN]).astype("int64").tolist())
    split_policy = {
        **split_policy,
        "test_timestamps_in_cv_folds": int(len(test_timestamps.intersection(cv_timestamps))),
    }
    scaler_payload = {
        "model_contract_version": MODEL_CONTRACT_VERSION,
        "feature_columns": FEATURE_COLUMNS,
        "raw_feature_columns": RAW_FEATURE_COLUMNS,
        "missing_indicator_columns": MISSING_INDICATOR_COLUMNS,
        "engineered_time_feature_columns": TIME_FEATURE_COLUMNS,
        "target_column": TARGET_COLUMN,
        "feature_fill_values": final_fill_values,
        "core_feature_min_present_fractions": CORE_FEATURE_MIN_PRESENT_FRACTIONS,
        "sen55_optional": True,
        "means": {col: final_scaler_stats[col]["mean"] for col in FEATURE_COLUMNS},
        "stds": {col: final_scaler_stats[col]["std"] for col in FEATURE_COLUMNS},
        "lookback": best_lookback,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "lookback_minutes": best_lookback_minutes,
        "lookback_rows": best_lookback,
        "source_data_path": portable_source_data_path,
        "source_data_format": source_data_format,
        "source_csv_path": portable_source_data_path if source_data_format == "csv" else None,
        "source_parquet_path": portable_source_data_path if source_data_format == "parquet" else None,
        "split_sizes": split_sizes,
        "split_policy": split_policy,
        "validation_mode": split_policy.get("validation_mode"),
        "allow_degenerate_validation": bool(allow_degenerate_validation),
        "cv_folds_requested": int(requested_cv_folds),
        "cv_folds_used": int(split_policy.get("cv_folds_used", len(cv_folds))),
        "min_strict_date_coverage": float(min_strict_date_coverage),
    }

    best_params_payload = {
        "model_contract_version": MODEL_CONTRACT_VERSION,
        "zone": ZONE_NUM,
        "study_name": study.study_name,
        "best_trial_number": int(study.best_trial.number),
        "best_mean_cv_pr_auc": float(study.best_value),
        "best_validation_pr_auc": float(study.best_value),
        "final_training_epochs": int(final_training_epochs),
        "final_training_epoch_policy": "ceil_median_best_cv_fold_epoch",
        "valid_lookback_candidates": lookback_candidates,
        "rejected_lookbacks": rejected_lookbacks,
        "allow_degenerate_validation": bool(allow_degenerate_validation),
        "bootstrap_fallback": bool(bootstrap_fallback),
        "cv_folds_requested": int(requested_cv_folds),
        "cv_folds_used": int(split_policy.get("cv_folds_used", len(cv_folds))),
        "validation_mode": split_policy.get("validation_mode"),
        "pos_weight": float(pos_weight),
        "cv_fold_metrics": cv_fold_metrics,
        "cv_pr_auc_std": study.best_trial.user_attrs.get("std_cv_pr_auc"),
        "params": _json_safe(best_params),
        "sen55_dropout_probability": DEFAULT_SEN55_DROPOUT_PROBABILITY,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "lookback_minutes": best_lookback_minutes,
        "lookback_rows": best_lookback,
    }
    metrics_payload = {
        "model_contract_version": MODEL_CONTRACT_VERSION,
        "zone": ZONE_NUM,
        "threshold_free": True,
        "raw_probability_output": True,
        "source_data_path": portable_source_data_path,
        "source_data_format": source_data_format,
        "source_csv_path": portable_source_data_path if source_data_format == "csv" else None,
        "source_parquet_path": portable_source_data_path if source_data_format == "parquet" else None,
        "lookback": best_lookback,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "lookback_minutes": best_lookback_minutes,
        "lookback_rows": best_lookback,
        "split_sizes": split_sizes,
        "split_policy": split_policy,
        "validation_mode": split_policy.get("validation_mode"),
        "allow_degenerate_validation": bool(allow_degenerate_validation),
        "cv_folds_requested": int(requested_cv_folds),
        "cv_folds_used": int(split_policy.get("cv_folds_used", len(cv_folds))),
        "min_strict_date_coverage": float(min_strict_date_coverage),
        "core_feature_min_present_fractions": CORE_FEATURE_MIN_PRESENT_FRACTIONS,
        "sen55_optional": True,
        "sen55_dropout_probability": DEFAULT_SEN55_DROPOUT_PROBABILITY,
        "cv_metrics": {
            "best_mean_pr_auc": float(study.best_value),
            "best_std_pr_auc": study.best_trial.user_attrs.get("std_cv_pr_auc"),
            "fold_metrics": cv_fold_metrics,
        },
        "metrics_by_split": metrics,
    }

    _write_json(best_params_payload, tables_dir / "best_params_zone_5.json")
    _write_json(scaler_payload, tables_dir / "scaler_stats_zone_5.json")
    _write_json(metrics_payload, tables_dir / "metrics_zone_5.json")

    expected_artifacts = [
        models_dir / "best_cnn_zone_5.pt",
        tables_dir / "best_params_zone_5.json",
        tables_dir / "scaler_stats_zone_5.json",
        tables_dir / "metrics_zone_5.json",
    ]
    for artifact in expected_artifacts:
        if not artifact.is_file():
            raise FileNotFoundError(f"Expected artifact missing after training: {artifact}")
        if artifact.stat().st_size == 0:
            raise ValueError(f"Expected artifact is empty: {artifact}")
    for json_path in [
        tables_dir / "best_params_zone_5.json",
        tables_dir / "scaler_stats_zone_5.json",
        tables_dir / "metrics_zone_5.json",
    ]:
        json.loads(json_path.read_text(encoding="utf-8"))
    try:
        torch.load(models_dir / "best_cnn_zone_5.pt", map_location="cpu", weights_only=True)
    except TypeError:
        torch.load(models_dir / "best_cnn_zone_5.pt", map_location="cpu")

    manifest_path = run_dir / RUN_MANIFEST_FILENAME
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_rev": _detect_git_rev(SCRIPT_DIR),
        "zone": ZONE_NUM,
        "model_contract_version": MODEL_CONTRACT_VERSION,
        "feature_columns": FEATURE_COLUMNS,
        "raw_feature_columns": RAW_FEATURE_COLUMNS,
        "missing_indicator_columns": MISSING_INDICATOR_COLUMNS,
        "engineered_time_feature_columns": TIME_FEATURE_COLUMNS,
        "target_column": TARGET_COLUMN,
        "lookback": best_lookback,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "lookback_minutes": best_lookback_minutes,
        "lookback_rows": best_lookback,
        "source_data_path": portable_source_data_path,
        "source_data_format": source_data_format,
        "validation_mode": split_policy.get("validation_mode"),
        "allow_degenerate_validation": bool(allow_degenerate_validation),
        "cv_folds_requested": int(requested_cv_folds),
        "cv_folds_used": int(split_policy.get("cv_folds_used", len(cv_folds))),
        "min_strict_date_coverage": float(min_strict_date_coverage),
        "final_training_epochs": int(final_training_epochs),
        "core_feature_min_present_fractions": CORE_FEATURE_MIN_PRESENT_FRACTIONS,
        "sen55_optional": True,
        "sen55_dropout_probability": DEFAULT_SEN55_DROPOUT_PROBABILITY,
        "split_policy": metrics_payload["split_policy"],
        "files": [
            {
                "relative_path": artifact.relative_to(run_dir).as_posix(),
                "size_bytes": int(artifact.stat().st_size),
                "sha256": _sha256_of_file(artifact),
            }
            for artifact in expected_artifacts
        ],
    }
    _write_json(manifest, manifest_path)
    _atomic_write_text(output_path / CURRENT_RUN_POINTER, run_id + "\n")

    return {
        "output_dir": str(output_path.resolve()),
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "current_run_pointer": str((output_path / CURRENT_RUN_POINTER).resolve()),
        "model_path": str(model_path.resolve()),
        "source_data_path": portable_source_data_path,
        "source_data_format": source_data_format,
        "source_csv_path": portable_source_data_path if source_data_format == "csv" else None,
        "source_parquet_path": portable_source_data_path if source_data_format == "parquet" else None,
        "best_params_path": str((tables_dir / "best_params_zone_5.json").resolve()),
        "scaler_stats_path": str((tables_dir / "scaler_stats_zone_5.json").resolve()),
        "metrics_path": str((tables_dir / "metrics_zone_5.json").resolve()),
        "best_mean_cv_pr_auc": float(study.best_value),
        "best_validation_pr_auc": float(study.best_value),
        "lookback": best_lookback,
        "validation_mode": split_policy.get("validation_mode"),
        "cv_folds_requested": int(requested_cv_folds),
        "cv_folds_used": int(split_policy.get("cv_folds_used", len(cv_folds))),
        "min_strict_date_coverage": float(min_strict_date_coverage),
        "final_training_epochs": int(final_training_epochs),
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "lookback_minutes": best_lookback_minutes,
        "lookback_rows": best_lookback,
        "device": device.type,
        "optuna_jobs": int(optuna_jobs),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train a zone 5 1D CNN from generated CSV or Parquet training data.")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Training CSV path. If no input is passed, newest CSV is preferred, then newest Parquet.",
    )
    source_group.add_argument(
        "--parquet",
        type=Path,
        default=None,
        help="Training Parquet path. If no input is passed, newest CSV is preferred, then newest Parquet.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Artifact output directory.")
    parser.add_argument("--n-trials", type=int, default=DEFAULT_N_TRIALS, help="Number of Optuna trials.")
    parser.add_argument("--optuna-jobs", type=int, default=None, help="Parallel Optuna jobs.")
    parser.add_argument("--max-epochs", type=int, default=DEFAULT_MAX_EPOCHS, help="Maximum epochs per trial.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Random seed.")
    parser.add_argument(
        "--cv-folds",
        type=int,
        choices=range(MIN_STRICT_CV_FOLDS, MAX_STRICT_CV_FOLDS + 1),
        default=MAX_STRICT_CV_FOLDS,
        metavar=f"{MIN_STRICT_CV_FOLDS},{MIN_STRICT_CV_FOLDS + 1},{MAX_STRICT_CV_FOLDS}",
        help="Number of one-day strict rolling validation folds to request. Default: 3.",
    )
    parser.add_argument(
        "--allow-bad-lines",
        action="store_true",
        help="Skip malformed CSV rows instead of failing fast.",
    )
    parser.add_argument(
        "--allow-degenerate-validation",
        action="store_true",
        help="Allow single-class validation windows. Intended only for smoke tests.",
    )
    parser.add_argument(
        "--bootstrap-fallback",
        action="store_true",
        help=(
            "If strict validation has no viable lookbacks, retry with one 80/20 chronological "
            "pre-test fold. Intended only to bootstrap the first production model."
        ),
    )
    parser.add_argument(
        "--min-strict-date-coverage",
        type=float,
        default=STRICT_DATE_MIN_COVERAGE,
        help="Minimum fraction of a full 10-second day required for a date to be used in strict CV. Default: 0.75.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = train_zone_5_from_csv(
        csv_path=args.csv,
        parquet_path=args.parquet,
        output_dir=args.output_dir,
        n_trials=args.n_trials,
        optuna_jobs=args.optuna_jobs,
        max_epochs=args.max_epochs,
        seed=args.seed,
        allow_bad_lines=args.allow_bad_lines,
        allow_degenerate_validation=args.allow_degenerate_validation,
        bootstrap_fallback=args.bootstrap_fallback,
        cv_folds=args.cv_folds,
        min_strict_date_coverage=args.min_strict_date_coverage,
    )
    print(json.dumps(_json_safe(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
