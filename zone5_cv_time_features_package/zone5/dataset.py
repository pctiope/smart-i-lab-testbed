from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from zone5 import csv_size_guard
from zone5.feature_contract import (
    CORE_FEATURE_MIN_PRESENT_FRACTIONS,
    FEATURE_COLUMNS,
    INPUT_CHANNEL_COUNT,
    LEGACY_TARGET_COLUMNS,
    LOOKBACK_ROWS_BY_MINUTES,
    MISSING_INDICATOR_COLUMNS,
    PACKAGE_ROOT,
    RAW_FEATURE_COLUMNS,
    SAMPLE_INTERVAL_SECONDS,
    SAMPLE_INTERVAL_PANDAS_FREQ,
    SEN55_FEATURE_COLUMNS,
    TARGET_COLUMN,
    TIME_FEATURE_COLUMNS,
    TIMESTAMP_COLUMN,
    MMWAVE_RECENCY_FEATURE_COLUMNS,
    add_mmwave_recency_features,
    add_time_features,
)


DEFAULT_TRAINING_CSV_DIR = PACKAGE_ROOT / "data"
DEFAULT_TRAINING_PARQUET_DIR = PACKAGE_ROOT / "data" / "training_snapshots"

TRAIN_FRAC = 0.70
VAL_FRAC = 0.15
TEST_FRAC = 0.15
BLIND_TEST_CALENDAR_DAYS = 1
CV_FOLDS = 3
CV_VALIDATION_CALENDAR_DAYS = 1
EMERGENCY_VALIDATION_FRAC = 0.20
STRICT_DATE_MIN_COVERAGE = 0.75
LOOKBACK_CHOICES = list(LOOKBACK_ROWS_BY_MINUTES.values())


def _latest_training_csv(training_csv_dir: Path = DEFAULT_TRAINING_CSV_DIR) -> Path:
    csv_files = sorted(
        (p for p in training_csv_dir.glob("*.csv") if not csv_size_guard.is_split_part_path(p)),
        key=lambda p: max(part.stat().st_mtime for part in csv_size_guard.existing_csv_parts(p) or [p]),
    )
    if not csv_files:
        raise FileNotFoundError(f"No training CSV files found in {training_csv_dir}")
    return csv_files[-1]


def _latest_training_parquet(training_parquet_dir: Path = DEFAULT_TRAINING_PARQUET_DIR) -> Path:
    required_columns = {TIMESTAMP_COLUMN, *RAW_FEATURE_COLUMNS, TARGET_COLUMN}
    parquet_files = sorted(
        (p for p in training_parquet_dir.glob("*.parquet") if _parquet_has_columns(p, required_columns)),
        key=lambda p: p.stat().st_mtime,
    )
    if not parquet_files:
        raise FileNotFoundError(
            f"No training Parquet files with required columns {sorted(required_columns)} found in {training_parquet_dir}"
        )
    return parquet_files[-1]


def _parquet_has_columns(path: Path, required_columns: set[str]) -> bool:
    try:
        import pyarrow.parquet as pq

        available = set(pq.read_schema(path).names)
    except Exception:
        try:
            available = set(pd.read_parquet(path).columns)
        except Exception:
            return False
    return required_columns.issubset(available)


def _csv_bad_line_report(path: Path) -> tuple[int, list[tuple[int, int]]]:
    bad_rows: list[tuple[int, int]] = []
    parts = csv_size_guard.existing_csv_parts(path) or [path]
    expected_width = 0
    for part in parts:
        with part.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            try:
                header = next(reader)
            except StopIteration:
                continue
            if expected_width == 0:
                expected_width = len(header)
            elif len(header) != expected_width:
                bad_rows.append((1, len(header)))
            for row in reader:
                if len(row) != expected_width:
                    bad_rows.append((reader.line_num, len(row)))
    return expected_width, bad_rows


def _expected_csv_width(path: Path) -> int:
    for part in csv_size_guard.existing_csv_parts(path) or [path]:
        with part.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            try:
                return len(next(reader))
            except StopIteration:
                continue
    return 0


def _read_csv_skipping_bad_lines(path: Path, bad_rows: list[tuple[int, int]]) -> pd.DataFrame:
    expected_width = _expected_csv_width(path)
    if expected_width == 0:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for part in csv_size_guard.existing_csv_parts(path) or [path]:
        with part.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            try:
                header = next(reader)
            except StopIteration:
                continue
            rows = [row for row in reader if len(row) == expected_width]
        if rows:
            frames.append(pd.DataFrame(rows, columns=header))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def _format_bad_lines(bad_rows: list[tuple[int, int]], limit: int = 5) -> str:
    return ", ".join(f"line {line}: {width} fields" for line, width in bad_rows[:limit])


def _target_column_in(raw: pd.DataFrame) -> str:
    if TARGET_COLUMN in raw.columns:
        return TARGET_COLUMN
    for candidate in LEGACY_TARGET_COLUMNS:
        if candidate in raw.columns:
            return candidate
    return TARGET_COLUMN


def _coerce_missing_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    for col in RAW_FEATURE_COLUMNS:
        if col not in prepared.columns:
            prepared[col] = np.nan
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce")

        indicator_col = f"{col}_missing"
        raw_missing = prepared[col].isna()
        if indicator_col in prepared.columns:
            indicator = pd.to_numeric(prepared[indicator_col], errors="coerce").fillna(0).astype(float) > 0
            missing = raw_missing | indicator
        else:
            missing = raw_missing
        prepared.loc[missing, col] = np.nan
        prepared[indicator_col] = missing.astype(int)
    return prepared


def _feature_present_values(frame: pd.DataFrame, feature_col: str) -> pd.Series:
    indicator_col = f"{feature_col}_missing"
    if indicator_col in frame.columns:
        missing = pd.to_numeric(frame[indicator_col], errors="coerce").fillna(1.0)
        return (1.0 - missing.clip(0.0, 1.0)).astype(float)
    if feature_col in frame.columns:
        return pd.to_numeric(frame[feature_col], errors="coerce").notna().astype(float)
    return pd.Series(np.zeros(len(frame), dtype=float), index=frame.index)


def feature_present_fraction(frame: pd.DataFrame, feature_col: str) -> float:
    if frame.empty:
        return 0.0
    present = _feature_present_values(frame, feature_col)
    return float(present.mean()) if len(present) else 0.0


def live_feature_quality_diagnostics(frame: pd.DataFrame, lookback: int) -> dict[str, Any]:
    prepared = _coerce_missing_indicators(frame)
    recent = prepared.tail(int(lookback)).copy()
    core_present_fractions = {
        col: feature_present_fraction(recent, col)
        for col in CORE_FEATURE_MIN_PRESENT_FRACTIONS
    }
    core_failures = [
        {
            "feature": col,
            "present_fraction": float(core_present_fractions[col]),
            "required_fraction": float(required),
        }
        for col, required in CORE_FEATURE_MIN_PRESENT_FRACTIONS.items()
        if core_present_fractions[col] < float(required)
    ]
    sen55_present_fractions = {
        col: feature_present_fraction(recent, col)
        for col in SEN55_FEATURE_COLUMNS
    }
    warnings: list[str] = []
    if sen55_present_fractions:
        max_sen55_present = max(sen55_present_fractions.values())
        min_sen55_present = min(sen55_present_fractions.values())
        if max_sen55_present <= 0.0:
            warnings.append("SEN55 unavailable; using neutral fill")
        elif min_sen55_present < 1.0:
            warnings.append("SEN55 partially unavailable; using neutral fill")
    return {
        "core_present_fractions": core_present_fractions,
        "core_failures": core_failures,
        "sen55_present_fractions": sen55_present_fractions,
        "warnings": warnings,
    }


def _window_core_quality_mask(frame: pd.DataFrame, lookback: int) -> np.ndarray:
    lookback = int(lookback)
    if len(frame) < lookback:
        return np.empty((0,), dtype=bool)
    prepared = _coerce_missing_indicators(frame)
    n_windows = len(prepared) - lookback + 1
    mask = np.ones(n_windows, dtype=bool)
    for col, required_fraction in CORE_FEATURE_MIN_PRESENT_FRACTIONS.items():
        present = _feature_present_values(prepared, col).to_numpy(dtype=np.float32)
        window_present = np.lib.stride_tricks.sliding_window_view(present, window_shape=lookback)
        mask &= window_present.mean(axis=1) >= float(required_fraction)
    return mask


def _clean_zone_5_training_frame(raw: pd.DataFrame, source_label: str) -> pd.DataFrame:
    target_source = _target_column_in(raw)
    required_columns = [TIMESTAMP_COLUMN, target_source]
    missing = [col for col in required_columns if col not in raw.columns]
    if missing:
        raise ValueError(f"{source_label} is missing required columns: {missing}")

    keep_columns = [
        TIMESTAMP_COLUMN,
        target_source,
        *[col for col in RAW_FEATURE_COLUMNS if col in raw.columns],
        *[f"{col}_missing" for col in RAW_FEATURE_COLUMNS if f"{col}_missing" in raw.columns],
    ]
    frame = raw[keep_columns].copy()
    if target_source != TARGET_COLUMN:
        frame = frame.rename(columns={target_source: TARGET_COLUMN})
    frame[TIMESTAMP_COLUMN] = pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce").dt.floor(
        SAMPLE_INTERVAL_PANDAS_FREQ
    )
    frame[TARGET_COLUMN] = pd.to_numeric(frame[TARGET_COLUMN], errors="coerce")
    frame = _coerce_missing_indicators(frame)

    frame = frame.dropna(subset=[TIMESTAMP_COLUMN])
    labeled_mask = frame[TARGET_COLUMN].notna()
    frame.loc[labeled_mask, TARGET_COLUMN] = frame.loc[labeled_mask, TARGET_COLUMN].round()
    invalid_label_mask = labeled_mask & ~frame[TARGET_COLUMN].isin([0, 1])
    frame.loc[invalid_label_mask, TARGET_COLUMN] = np.nan
    frame = frame.sort_values(TIMESTAMP_COLUMN)
    frame = frame.drop_duplicates(subset=[TIMESTAMP_COLUMN], keep="last")
    frame = frame.reset_index(drop=True)
    frame = add_mmwave_recency_features(frame)
    frame = add_time_features(frame)

    labeled_rows = int(frame[TARGET_COLUMN].notna().sum())
    if frame.empty or labeled_rows == 0:
        raise ValueError(
            "No labeled zone 5 rows remain after parsing timestamps and target values. "
            "Rows with null feature values are retained, but supervised training needs at least one CV label."
        )
    return frame


def load_zone_5_csv(
    csv_path: str | Path | None = None,
    allow_bad_lines: bool = False,
) -> tuple[pd.DataFrame, Path]:
    path = Path(csv_path) if csv_path is not None else _latest_training_csv()
    if not path.exists():
        raise FileNotFoundError(f"Training CSV not found: {path}")

    _, bad_rows = _csv_bad_line_report(path)
    if bad_rows and not allow_bad_lines:
        raise ValueError(
            f"Malformed CSV rows detected in {path}. "
            f"Bad rows: {_format_bad_lines(bad_rows)}. "
            "Regenerate the CSV or pass allow_bad_lines=True / --allow-bad-lines to skip malformed rows."
        )
    if bad_rows:
        print(f"Warning: skipping {len(bad_rows)} malformed CSV row(s): {_format_bad_lines(bad_rows)}")
        raw = _read_csv_skipping_bad_lines(path, bad_rows)
    else:
        raw = csv_size_guard.read_csv_parts(path)

    return _clean_zone_5_training_frame(raw, "CSV"), path


def load_zone_5_parquet(parquet_path: str | Path | None = None) -> tuple[pd.DataFrame, Path]:
    path = Path(parquet_path) if parquet_path is not None else _latest_training_parquet()
    if not path.exists():
        raise FileNotFoundError(f"Training Parquet not found: {path}")
    raw = pd.read_parquet(path)
    return _clean_zone_5_training_frame(raw, "Parquet"), path


def load_zone_5_training_data(
    csv_path: str | Path | None = None,
    parquet_path: str | Path | None = None,
    allow_bad_lines: bool = False,
) -> tuple[pd.DataFrame, Path, str]:
    if csv_path is not None and parquet_path is not None:
        raise ValueError("Pass either csv_path or parquet_path, not both.")
    if parquet_path is not None:
        frame, path = load_zone_5_parquet(parquet_path)
        return frame, path, "parquet"
    if csv_path is not None:
        frame, path = load_zone_5_csv(csv_path, allow_bad_lines=allow_bad_lines)
        return frame, path, "csv"

    try:
        frame, path = load_zone_5_csv(allow_bad_lines=allow_bad_lines)
        return frame, path, "csv"
    except FileNotFoundError:
        frame, path = load_zone_5_parquet()
        return frame, path, "parquet"


def _calendar_dates(frame: pd.DataFrame) -> list[pd.Timestamp]:
    dates = pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce").dt.normalize()
    return sorted(pd.Timestamp(value) for value in dates.dropna().unique())


def _rows_per_full_day() -> int:
    return int(math.ceil(pd.Timedelta(days=1).total_seconds() / float(SAMPLE_INTERVAL_SECONDS)))


def _strict_date_coverage(
    frame: pd.DataFrame,
    min_coverage: float = STRICT_DATE_MIN_COVERAGE,
) -> tuple[list[pd.Timestamp], list[dict[str, Any]]]:
    dates = _calendar_dates(frame)
    if min_coverage <= 0:
        return dates, []

    date_series = pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce").dt.normalize()
    required_rows = int(math.ceil(_rows_per_full_day() * float(min_coverage)))
    eligible: list[pd.Timestamp] = []
    excluded: list[dict[str, Any]] = []
    for date in dates:
        rows = int((date_series == date).sum())
        coverage = rows / float(_rows_per_full_day())
        if rows >= required_rows:
            eligible.append(date)
        else:
            excluded.append(
                {
                    "date": date.date().isoformat(),
                    "rows": rows,
                    "coverage": float(coverage),
                    "required_rows": required_rows,
                    "reason": "strict_cv_date_under_min_coverage",
                }
            )
    return eligible, excluded


def _frame_time_bounds(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"rows": 0, "start": None, "end": None, "start_date": None, "end_date": None}
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN])
    return {
        "rows": int(len(frame)),
        "start": timestamps.min().isoformat(sep=" "),
        "end": timestamps.max().isoformat(sep=" "),
        "start_date": timestamps.min().normalize().date().isoformat(),
        "end_date": timestamps.max().normalize().date().isoformat(),
    }


def blind_test_split(
    frame: pd.DataFrame,
    test_calendar_days: int = BLIND_TEST_CALENDAR_DAYS,
) -> dict[str, pd.DataFrame]:
    test_calendar_days = int(test_calendar_days)
    if test_calendar_days < 1:
        raise ValueError(f"test_calendar_days must be at least 1; got {test_calendar_days}.")
    unique_dates = _calendar_dates(frame)
    if len(unique_dates) <= test_calendar_days:
        if test_calendar_days == 1:
            raise ValueError(
                "At least 2 calendar days are required for a true 1-day hidden test; "
                f"found {len(unique_dates)}."
            )
        raise ValueError(
            f"Need more than {test_calendar_days} calendar day(s) to hold out a blind test split; "
            f"found {len(unique_dates)}."
        )
    test_start_date = unique_dates[-test_calendar_days]
    date_series = pd.to_datetime(frame[TIMESTAMP_COLUMN]).dt.normalize()
    splits = {
        "pre_test": frame.loc[date_series < test_start_date].copy().reset_index(drop=True),
        "test": frame.loc[date_series >= test_start_date].copy().reset_index(drop=True),
    }
    empty_splits = [name for name, split in splits.items() if split.empty]
    if empty_splits:
        raise ValueError(f"Blind test split produced empty split(s): {empty_splits}")
    return splits


def rolling_validation_folds(
    pre_test_frame: pd.DataFrame,
    n_folds: int = CV_FOLDS,
    validation_calendar_days: int = CV_VALIDATION_CALENDAR_DAYS,
    min_date_coverage: float = STRICT_DATE_MIN_COVERAGE,
) -> list[dict[str, Any]]:
    n_folds = int(n_folds)
    validation_calendar_days = int(validation_calendar_days)
    if n_folds < 1:
        raise ValueError(f"n_folds must be at least 1; got {n_folds}.")
    if validation_calendar_days < 1:
        raise ValueError(f"validation_calendar_days must be at least 1; got {validation_calendar_days}.")

    unique_dates, _excluded_dates = _strict_date_coverage(pre_test_frame, min_coverage=min_date_coverage)
    max_folds_from_dates = max(0, (len(unique_dates) - 1) // validation_calendar_days)
    folds_used = min(n_folds, max_folds_from_dates)
    if folds_used < 1:
        raise ValueError(
            "Need at least two coverage-eligible pre-test calendar dates for rolling validation; "
            f"found {len(unique_dates)} eligible date(s)."
        )

    required_validation_dates = folds_used * validation_calendar_days
    first_validation_idx = len(unique_dates) - required_validation_dates
    date_series = pd.to_datetime(pre_test_frame[TIMESTAMP_COLUMN]).dt.normalize()
    folds: list[dict[str, Any]] = []
    for fold_idx in range(folds_used):
        start_idx = first_validation_idx + fold_idx * validation_calendar_days
        end_idx = start_idx + validation_calendar_days
        validation_dates = unique_dates[start_idx:end_idx]
        val_start_date = validation_dates[0]
        val_end_exclusive = validation_dates[-1] + pd.Timedelta(days=1)
        eligible_set = set(unique_dates)
        train_mask = (date_series < val_start_date) & date_series.isin(eligible_set)
        val_mask = (date_series >= val_start_date) & (date_series < val_end_exclusive)
        train_df = pre_test_frame.loc[train_mask].copy().reset_index(drop=True)
        val_df = pre_test_frame.loc[val_mask].copy().reset_index(drop=True)
        if train_df.empty or val_df.empty:
            raise ValueError(
                f"Rolling fold {fold_idx + 1} produced train_rows={len(train_df)} and val_rows={len(val_df)}"
            )
        folds.append(
            {
                "name": f"fold_{fold_idx + 1}",
                "validation_mode": "rolling_calendar",
                "train": train_df,
                "val": val_df,
                "validation_start_date": val_start_date.date().isoformat(),
                "validation_end_date": validation_dates[-1].date().isoformat(),
                "train_bounds": _frame_time_bounds(train_df),
                "validation_bounds": _frame_time_bounds(val_df),
            }
        )
    return folds


def chronological_validation_fold(
    pre_test_frame: pd.DataFrame,
    validation_frac: float = EMERGENCY_VALIDATION_FRAC,
    validation_mode: str = "chronological",
    error_label: str = "Chronological validation",
) -> dict[str, Any]:
    ordered = pre_test_frame.sort_values(TIMESTAMP_COLUMN).reset_index(drop=True)
    n_rows = len(ordered)
    if n_rows < 2:
        raise ValueError(
            f"{error_label} requires at least 2 rows before the hidden test day; "
            f"found {n_rows}."
        )

    validation_rows = max(1, int(math.ceil(n_rows * float(validation_frac))))
    validation_rows = min(validation_rows, n_rows - 1)
    train_end = n_rows - validation_rows
    train_df = ordered.iloc[:train_end].copy().reset_index(drop=True)
    val_df = ordered.iloc[train_end:].copy().reset_index(drop=True)
    if train_df.empty or val_df.empty:
        raise ValueError(
            f"{error_label} produced train_rows={len(train_df)} and val_rows={len(val_df)}"
        )

    validation_bounds = _frame_time_bounds(val_df)
    return {
        "name": "fold_1",
        "validation_mode": validation_mode,
        "train": train_df,
        "val": val_df,
        "validation_start_date": validation_bounds["start_date"],
        "validation_end_date": validation_bounds["end_date"],
        "train_bounds": _frame_time_bounds(train_df),
        "validation_bounds": validation_bounds,
    }


def emergency_chronological_validation_fold(
    pre_test_frame: pd.DataFrame,
    validation_frac: float = EMERGENCY_VALIDATION_FRAC,
) -> dict[str, Any]:
    return chronological_validation_fold(
        pre_test_frame,
        validation_frac=validation_frac,
        validation_mode="emergency_chronological",
        error_label="Emergency validation",
    )


def bootstrap_chronological_validation_fold(
    pre_test_frame: pd.DataFrame,
    validation_frac: float = EMERGENCY_VALIDATION_FRAC,
) -> dict[str, Any]:
    return chronological_validation_fold(
        pre_test_frame,
        validation_frac=validation_frac,
        validation_mode="bootstrap_chronological_fallback",
        error_label="Bootstrap fallback validation",
    )


def _cv_fold_bounds(cv_folds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "fold": fold["name"],
            "validation_mode": fold.get("validation_mode", "rolling_calendar"),
            "validation_start_date": fold["validation_start_date"],
            "validation_end_date": fold["validation_end_date"],
            "train_bounds": fold["train_bounds"],
            "validation_bounds": fold["validation_bounds"],
        }
        for fold in cv_folds
    ]


def validation_folds_for_blind_split(
    blind_splits: dict[str, pd.DataFrame],
    n_folds: int = CV_FOLDS,
    validation_calendar_days: int = CV_VALIDATION_CALENDAR_DAYS,
    test_calendar_days: int = BLIND_TEST_CALENDAR_DAYS,
    min_strict_date_coverage: float = STRICT_DATE_MIN_COVERAGE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pre_test_dates = _calendar_dates(blind_splits["pre_test"])
    eligible_dates, excluded_dates = _strict_date_coverage(
        blind_splits["pre_test"],
        min_coverage=float(min_strict_date_coverage),
    )
    if len(pre_test_dates) >= 2:
        cv_folds = rolling_validation_folds(
            blind_splits["pre_test"],
            n_folds=n_folds,
            validation_calendar_days=validation_calendar_days,
            min_date_coverage=min_strict_date_coverage,
        )
        validation_mode = "rolling_calendar"
    elif len(pre_test_dates) == 1:
        cv_folds = [emergency_chronological_validation_fold(blind_splits["pre_test"])]
        validation_mode = "emergency_chronological"
    else:
        raise ValueError(
            "At least 2 calendar days are required for a true 1-day hidden test; "
            f"found {len(_calendar_dates(pd.concat([blind_splits['pre_test'], blind_splits['test']], ignore_index=True)))}."
        )

    split_policy = {
        "validation_mode": validation_mode,
        "blind_test_calendar_days": int(test_calendar_days),
        "cv_folds": int(len(cv_folds)),
        "cv_folds_requested": int(n_folds),
        "cv_folds_used": int(len(cv_folds)),
        "cv_validation_calendar_days": int(validation_calendar_days),
        "strict_date_min_coverage": float(min_strict_date_coverage),
        "strict_date_full_day_rows": int(_rows_per_full_day()),
        "strict_date_eligible_dates": [date.date().isoformat() for date in eligible_dates],
        "strict_date_excluded_dates": excluded_dates,
        "final_model_training_rows": "all rows before the blind test split",
        "pre_test_bounds": _frame_time_bounds(blind_splits["pre_test"]),
        "blind_test_bounds": _frame_time_bounds(blind_splits["test"]),
        "cv_fold_bounds": _cv_fold_bounds(cv_folds),
    }
    return cv_folds, split_policy


def blind_test_and_validation_splits(
    frame: pd.DataFrame,
    test_calendar_days: int = BLIND_TEST_CALENDAR_DAYS,
    n_folds: int = CV_FOLDS,
    validation_calendar_days: int = CV_VALIDATION_CALENDAR_DAYS,
    min_strict_date_coverage: float = STRICT_DATE_MIN_COVERAGE,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, Any]], dict[str, Any]]:
    blind_splits = blind_test_split(frame, test_calendar_days=test_calendar_days)
    cv_folds, split_policy = validation_folds_for_blind_split(
        blind_splits,
        n_folds=n_folds,
        validation_calendar_days=validation_calendar_days,
        test_calendar_days=test_calendar_days,
        min_strict_date_coverage=min_strict_date_coverage,
    )
    return blind_splits, cv_folds, split_policy


def chronological_split(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    n_rows = len(frame)
    train_end = int(n_rows * TRAIN_FRAC)
    val_end = int(n_rows * (TRAIN_FRAC + VAL_FRAC))
    splits = {
        "train": frame.iloc[:train_end].copy(),
        "val": frame.iloc[train_end:val_end].copy(),
        "test": frame.iloc[val_end:].copy(),
    }
    empty_splits = [name for name, split in splits.items() if split.empty]
    if empty_splits:
        raise ValueError(f"Chronological split produced empty split(s): {empty_splits}")
    return splits


def fill_values_from_train(train_df: pd.DataFrame) -> dict[str, float]:
    fill_values: dict[str, float] = {}
    for col in RAW_FEATURE_COLUMNS:
        series = pd.to_numeric(train_df[col], errors="coerce") if col in train_df.columns else pd.Series(dtype=float)
        median = float(series.median()) if not series.dropna().empty else 0.0
        if not math.isfinite(median):
            median = 0.0
        fill_values[col] = median
    return fill_values


def apply_training_preprocessing(frame: pd.DataFrame, fill_values: dict[str, float]) -> pd.DataFrame:
    prepared = _coerce_missing_indicators(frame)
    prepared = add_mmwave_recency_features(prepared)
    if any(col not in prepared.columns for col in TIME_FEATURE_COLUMNS):
        prepared = add_time_features(prepared)

    for col in RAW_FEATURE_COLUMNS:
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce").fillna(float(fill_values.get(col, 0.0)))
    for col in MMWAVE_RECENCY_FEATURE_COLUMNS:
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce")
    for col in MISSING_INDICATOR_COLUMNS:
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce").fillna(1).round().clip(0, 1).astype(int)
    for col in TIME_FEATURE_COLUMNS:
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce")

    prepared = prepared.dropna(subset=FEATURE_COLUMNS)
    return prepared.reset_index(drop=True)


def scaler_from_train(train_df: pd.DataFrame) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for col in FEATURE_COLUMNS:
        mean = float(train_df[col].mean())
        std = float(train_df[col].std(ddof=0))
        if not math.isfinite(std) or std == 0.0:
            std = 1.0
        stats[col] = {"mean": mean, "std": std}
    return stats


def apply_scaler(frame: pd.DataFrame, stats: dict[str, dict[str, float]]) -> pd.DataFrame:
    scaled = frame.copy()
    for col in FEATURE_COLUMNS:
        scaled[col] = (scaled[col] - stats[col]["mean"]) / stats[col]["std"]
    return scaled


def prepare_and_standardize_splits(
    splits: dict[str, pd.DataFrame],
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, float]], dict[str, float]]:
    fill_values = fill_values_from_train(splits["train"])
    prepared_splits = {
        name: apply_training_preprocessing(split, fill_values)
        for name, split in splits.items()
    }
    stats = scaler_from_train(prepared_splits["train"])
    neutral_values = {
        col: (float(fill_values.get(col, 0.0)) - stats[col]["mean"]) / stats[col]["std"]
        for col in RAW_FEATURE_COLUMNS
    }
    scaled_splits = {name: apply_scaler(split, stats) for name, split in prepared_splits.items()}
    for split in scaled_splits.values():
        split.attrs["feature_neutral_values"] = neutral_values
    return scaled_splits, stats, fill_values


def standardize_splits(
    splits: dict[str, pd.DataFrame],
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, float]]]:
    scaled_splits, stats, _fill_values = prepare_and_standardize_splits(splits)
    return scaled_splits, stats


def valid_lookback_candidates(splits: dict[str, pd.DataFrame]) -> list[int]:
    max_fit = min(len(split) for split in splits.values())
    return [lookback for lookback in LOOKBACK_CHOICES if lookback <= max_fit]


def _window_labels_for_lookback(
    frame: pd.DataFrame,
    lookback: int,
    require_core_quality: bool = True,
) -> np.ndarray:
    labels = frame[TARGET_COLUMN].to_numpy(dtype=np.float32)
    if len(labels) < lookback:
        return np.empty((0,), dtype=np.float32)
    window_labels = labels[lookback - 1 :].astype(np.float32, copy=False)
    valid_mask = ~np.isnan(window_labels)
    if require_core_quality:
        quality_mask = _window_core_quality_mask(frame, lookback)
        if quality_mask.size:
            valid_mask &= quality_mask
    return window_labels[valid_mask]


def viable_lookback_candidates(
    splits: dict[str, pd.DataFrame],
    allow_degenerate_validation: bool = False,
) -> tuple[list[int], dict[int, str]]:
    candidates = valid_lookback_candidates(splits)
    rejected: dict[int, str] = {}
    viable: list[int] = []

    for lookback in candidates:
        train_y = _window_labels_for_lookback(splits["train"], lookback)
        val_y = _window_labels_for_lookback(splits["val"], lookback)
        if np.unique(train_y.astype(int)).size < 2:
            rejected[lookback] = "train_windows_single_class"
            continue
        if not allow_degenerate_validation and np.unique(val_y.astype(int)).size < 2:
            rejected[lookback] = "val_windows_single_class"
            continue
        viable.append(lookback)

    return viable, rejected


def make_windows(frame: pd.DataFrame, lookback: int) -> tuple[np.ndarray, np.ndarray, pd.Series]:
    values = frame[FEATURE_COLUMNS].to_numpy(dtype=np.float32)
    labels = frame[TARGET_COLUMN].to_numpy(dtype=np.float32)
    timestamps = frame[TIMESTAMP_COLUMN].reset_index(drop=True)

    if len(frame) < lookback:
        return (
            np.empty((0, INPUT_CHANNEL_COUNT, lookback), dtype=np.float32),
            np.empty((0,), dtype=np.float32),
            timestamps.iloc[0:0],
        )

    X = np.lib.stride_tricks.sliding_window_view(values, window_shape=lookback, axis=0)
    X = np.ascontiguousarray(X, dtype=np.float32)
    y_all = labels[lookback - 1 :].astype(np.float32, copy=False)
    valid_mask = ~np.isnan(y_all)
    quality_mask = _window_core_quality_mask(frame, lookback)
    if quality_mask.size:
        valid_mask &= quality_mask
    X = X[valid_mask]
    y = y_all[valid_mask]
    ts = timestamps.iloc[lookback - 1 :].reset_index(drop=True).iloc[valid_mask].reset_index(drop=True)
    return X, y, ts


def build_split_windows(
    scaled_splits: dict[str, pd.DataFrame],
    lookback: int,
) -> dict[str, dict[str, Any]]:
    return {
        split_name: {
            "X": X_split,
            "y": y_split,
            "timestamps": ts_split,
            "sen55_neutral_values": [
                float(split_df.attrs.get("feature_neutral_values", {}).get(col, 0.0))
                for col in SEN55_FEATURE_COLUMNS
                if col in FEATURE_COLUMNS
            ],
        }
        for split_name, split_df in scaled_splits.items()
        for X_split, y_split, ts_split in [make_windows(split_df, lookback)]
    }


def safe_pr_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    positives = int(np.asarray(y_true, dtype=int).sum())
    if positives == 0:
        return 0.0
    if positives == y_true.size:
        return 1.0
    try:
        return float(average_precision_score(y_true.astype(int), scores))
    except ValueError:
        return float("nan")


def safe_roc_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    if y_true.size == 0 or np.unique(y_true.astype(int)).size < 2:
        return float("nan")
    try:
        return float(roc_auc_score(y_true.astype(int), scores))
    except ValueError:
        return float("nan")


def split_metrics(y_true: np.ndarray, scores: np.ndarray) -> dict[str, float | int]:
    probs = np.clip(np.asarray(scores, dtype=np.float64), 1e-7, 1.0 - 1e-7)
    y = np.asarray(y_true, dtype=int)
    return {
        "n_windows": int(y.size),
        "positive_windows": int(y.sum()) if y.size else 0,
        "negative_windows": int(y.size - y.sum()) if y.size else 0,
        "positive_rate": float(y.mean()) if y.size else float("nan"),
        "mean_occupancy_probability": float(probs.mean()) if probs.size else float("nan"),
        "pr_auc": safe_pr_auc(y, probs),
        "roc_auc": safe_roc_auc(y, probs),
        "bce_log_loss": float(log_loss(y, probs, labels=[0, 1])) if y.size else float("nan"),
        "brier_score": float(brier_score_loss(y, probs)) if y.size else float("nan"),
    }


def label_evidence_counts(frame: pd.DataFrame) -> dict[str, int]:
    if frame.empty or TARGET_COLUMN not in frame.columns:
        return {"positive_buckets": 0, "negative_buckets": 0, "positive_events": 0}

    ordered = frame.copy()
    ordered[TIMESTAMP_COLUMN] = pd.to_datetime(ordered[TIMESTAMP_COLUMN], errors="coerce")
    ordered = ordered.dropna(subset=[TIMESTAMP_COLUMN]).sort_values(TIMESTAMP_COLUMN).reset_index(drop=True)
    labels = pd.to_numeric(ordered[TARGET_COLUMN], errors="coerce")
    positives = labels == 1
    negatives = labels == 0
    events = 0
    previous_ts: pd.Timestamp | None = None
    previous_positive = False
    max_gap = pd.Timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    for idx, is_positive in enumerate(positives.tolist()):
        ts = pd.Timestamp(ordered.loc[idx, TIMESTAMP_COLUMN])
        gap_break = previous_ts is not None and (ts - previous_ts) > max_gap
        if bool(is_positive) and (not previous_positive or gap_break):
            events += 1
        previous_positive = bool(is_positive)
        previous_ts = ts
    return {
        "positive_buckets": int(positives.sum()),
        "negative_buckets": int(negatives.sum()),
        "positive_events": int(events),
    }


def blind_test_evidence_by_lookback(
    test_frame: pd.DataFrame,
    lookback_candidates: list[int],
) -> dict[str, Any]:
    positive_windows_by_lookback: dict[int, int] = {}
    for lookback in lookback_candidates:
        labels = _window_labels_for_lookback(test_frame, int(lookback), require_core_quality=False)
        positive_windows_by_lookback[int(lookback)] = int(labels.astype(int).sum())
    evidence = label_evidence_counts(test_frame)
    evidence.update(
        {
            "positive_windows_by_lookback": positive_windows_by_lookback,
            "max_positive_windows": max(positive_windows_by_lookback.values())
            if positive_windows_by_lookback
            else 0,
        }
    )
    return evidence
