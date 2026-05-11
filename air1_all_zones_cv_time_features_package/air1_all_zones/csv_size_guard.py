from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd


DEFAULT_MAX_CSV_BYTES = int(os.environ.get("AIR1_ALL_ZONES_MAX_CSV_BYTES", "1000000000"))
_PART_RE = re.compile(r"^(?P<base>.+)_part(?P<number>\d+)$")


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return value


def _render_csv_row(fieldnames: Sequence[str], row: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writerow({field: _csv_value(row.get(field, "")) for field in fieldnames})
    return output.getvalue()


def _render_csv_header(fieldnames: Sequence[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(list(fieldnames))
    return output.getvalue()


def split_part_path(base_path: str | Path, part_number: int) -> Path:
    path = Path(base_path)
    if part_number <= 1:
        return path
    return path.with_name(f"{path.stem}_part{part_number:04d}{path.suffix}")


def split_part_number(path: str | Path, base_path: str | Path) -> int | None:
    candidate = Path(path)
    base = Path(base_path)
    if candidate == base:
        return 1
    if candidate.suffix != base.suffix:
        return None
    match = _PART_RE.match(candidate.stem)
    if not match or match.group("base") != base.stem:
        return None
    try:
        return int(match.group("number"))
    except ValueError:
        return None


def is_split_part_path(path: str | Path) -> bool:
    candidate = Path(path)
    return candidate.suffix.lower() == ".csv" and _PART_RE.match(candidate.stem) is not None


def base_path_for_split_part(path: str | Path) -> Path:
    candidate = Path(path)
    match = _PART_RE.match(candidate.stem)
    if not match:
        return candidate
    return candidate.with_name(f"{match.group('base')}{candidate.suffix}")


def existing_csv_parts(base_path: str | Path) -> list[Path]:
    base = Path(base_path)
    parts: list[Path] = []
    if base.is_file():
        parts.append(base)
    if base.parent.exists():
        for candidate in base.parent.glob(f"{base.stem}_part*{base.suffix}"):
            if split_part_number(candidate, base) is not None:
                parts.append(candidate)
    return sorted(set(parts), key=lambda item: split_part_number(item, base) or 0)


def has_csv_data(base_path: str | Path) -> bool:
    return any(path.is_file() and path.stat().st_size > 0 for path in existing_csv_parts(base_path))


def has_csv_rows(base_path: str | Path) -> bool:
    for path in existing_csv_parts(base_path):
        if path.stat().st_size == 0:
            continue
        with path.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            try:
                next(reader)
            except StopIteration:
                continue
            try:
                next(reader)
                return True
            except StopIteration:
                continue
    return False


def remove_stale_split_parts(base_path: str | Path, keep_paths: Iterable[str | Path] = ()) -> None:
    base = Path(base_path)
    keep = {Path(path).resolve() for path in keep_paths}
    for path in existing_csv_parts(base):
        if path == base:
            continue
        if path.resolve() not in keep:
            path.unlink(missing_ok=True)


def stale_split_parts(base_path: str | Path) -> list[Path]:
    base = Path(base_path)
    return [path for path in existing_csv_parts(base) if path != base]


def csv_parts_metadata(base_path: str | Path, package_root: str | Path | None = None) -> list[dict[str, Any]]:
    root = Path(package_root).resolve() if package_root is not None else None
    metadata: list[dict[str, Any]] = []
    for path in existing_csv_parts(base_path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        display_path = path.resolve()
        if root is not None:
            try:
                display = display_path.relative_to(root).as_posix()
            except ValueError:
                display = str(display_path)
        else:
            display = str(display_path)
        metadata.append(
            {
                "path": display,
                "size_bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return metadata


def read_csv_parts(base_path: str | Path, **kwargs: Any) -> pd.DataFrame:
    parts = existing_csv_parts(base_path)
    if not parts:
        return pd.read_csv(base_path, **kwargs)
    frames: list[pd.DataFrame] = []
    for path in parts:
        if path.stat().st_size == 0:
            continue
        try:
            frames.append(pd.read_csv(path, **kwargs))
        except pd.errors.EmptyDataError:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def iter_dict_rows(base_path: str | Path) -> Iterable[dict[str, Any]]:
    for path in existing_csv_parts(base_path):
        if path.stat().st_size == 0:
            continue
        with path.open("r", newline="", encoding="utf-8") as csvfile:
            yield from csv.DictReader(csvfile)


class RotatingCsvWriter:
    def __init__(
        self,
        base_path: str | Path,
        fieldnames: Sequence[str],
        *,
        max_bytes: int = DEFAULT_MAX_CSV_BYTES,
        mode: str = "a",
        extrasaction: str = "ignore",
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than 0")
        self.base_path = Path(base_path)
        self.fieldnames = list(fieldnames)
        self.max_bytes = int(max_bytes)
        self.mode = mode
        self.extrasaction = extrasaction
        self.header_text = _render_csv_header(self.fieldnames)
        self.header_bytes = len(self.header_text.encode("utf-8"))
        if self.header_bytes > self.max_bytes:
            raise ValueError(
                f"CSV header is {self.header_bytes} bytes, larger than the configured limit {self.max_bytes}"
            )
        self._handle: Any | None = None
        self._writer: csv.DictWriter[str] | None = None
        self._current_size = 0
        self._current_part = 1
        self._written_paths: list[Path] = []
        self.base_path.parent.mkdir(parents=True, exist_ok=True)
        self._open_initial()

    @property
    def written_paths(self) -> list[Path]:
        return list(dict.fromkeys(self._written_paths))

    def _open_initial(self) -> None:
        if self.mode == "w":
            for path in existing_csv_parts(self.base_path):
                path.unlink(missing_ok=True)
            self._open_part(1, append=False)
            return
        if self.mode != "a":
            raise ValueError("RotatingCsvWriter mode must be 'a' or 'w'")

        parts = existing_csv_parts(self.base_path)
        if not parts:
            self._open_part(1, append=False)
            return
        last = parts[-1]
        part_number = split_part_number(last, self.base_path) or 1
        if last.stat().st_size >= self.max_bytes:
            self._open_part(part_number + 1, append=False)
        else:
            self._open_part(part_number, append=True)

    def _open_part(self, part_number: int, *, append: bool) -> None:
        path = split_part_path(self.base_path, part_number)
        mode = "a" if append and path.exists() and path.stat().st_size > 0 else "w"
        self._handle = path.open(mode, newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._handle,
            fieldnames=self.fieldnames,
            extrasaction=self.extrasaction,
        )
        self._current_part = part_number
        if mode == "w":
            self._handle.write(self.header_text)
            self._current_size = self.header_bytes
        else:
            self._current_size = path.stat().st_size
        self._written_paths.append(path)

    def _rotate(self) -> None:
        self.close()
        self._open_part(self._current_part + 1, append=False)

    def writerow(self, row: dict[str, Any]) -> Path:
        row_text = _render_csv_row(self.fieldnames, row)
        row_bytes = len(row_text.encode("utf-8"))
        if self.header_bytes + row_bytes > self.max_bytes:
            raise ValueError(
                f"One CSV row needs {self.header_bytes + row_bytes} bytes including the header, "
                f"larger than the configured limit {self.max_bytes}"
            )
        if self._current_size + row_bytes > self.max_bytes:
            if self._current_size <= self.header_bytes:
                raise ValueError(
                    f"Cannot fit CSV row in an empty part under the configured limit {self.max_bytes}"
                )
            self._rotate()
        assert self._handle is not None
        assert self._writer is not None
        cleaned = {field: _csv_value(row.get(field, "")) for field in self.fieldnames}
        self._writer.writerow(cleaned)
        self._current_size += row_bytes
        return split_part_path(self.base_path, self._current_part)

    def writerows(self, rows: Iterable[dict[str, Any]]) -> list[Path]:
        for row in rows:
            self.writerow(row)
        return self.written_paths

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._writer = None

    def __enter__(self) -> "RotatingCsvWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def append_rows_split(
    base_path: str | Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
    extrasaction: str = "ignore",
) -> list[Path]:
    with RotatingCsvWriter(
        base_path,
        fieldnames,
        max_bytes=max_bytes,
        mode="a",
        extrasaction=extrasaction,
    ) as writer:
        return writer.writerows(rows)


def write_rows_split(
    base_path: str | Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
    extrasaction: str = "ignore",
) -> list[Path]:
    with RotatingCsvWriter(
        base_path,
        fieldnames,
        max_bytes=max_bytes,
        mode="w",
        extrasaction=extrasaction,
    ) as writer:
        return writer.writerows(rows)


def dataframe_rows(frame: pd.DataFrame) -> Iterable[dict[str, Any]]:
    columns = list(frame.columns)
    for values in frame.itertuples(index=False, name=None):
        yield {column: _csv_value(value) for column, value in zip(columns, values)}


def write_dataframe_split(
    frame: pd.DataFrame,
    base_path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> list[Path]:
    return write_rows_split(
        base_path,
        list(frame.columns),
        dataframe_rows(frame),
        max_bytes=max_bytes,
    )


def _temp_split_base(base_path: Path) -> Path:
    token = uuid.uuid4().hex
    return base_path.with_name(f".{base_path.stem}.{token}.tmp{base_path.suffix}")


def _cleanup_temp_parts(temp_base: Path) -> None:
    for path in existing_csv_parts(temp_base):
        path.unlink(missing_ok=True)


def _replace_with_temp_parts(base_path: Path, temp_base: Path, temp_parts: list[Path]) -> list[Path]:
    final_parts = [split_part_path(base_path, index + 1) for index in range(len(temp_parts))]
    for path in existing_csv_parts(base_path):
        path.unlink(missing_ok=True)
    for temp_part, final_part in zip(temp_parts, final_parts):
        os.replace(temp_part, final_part)
    remove_stale_split_parts(base_path, keep_paths=final_parts)
    return final_parts


def write_dataframe_split_atomic(
    frame: pd.DataFrame,
    base_path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> list[Path]:
    path = Path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_base = _temp_split_base(path)
    try:
        temp_parts = write_dataframe_split(frame, temp_base, max_bytes=max_bytes)
        return _replace_with_temp_parts(path, temp_base, temp_parts)
    finally:
        _cleanup_temp_parts(temp_base)


def write_rows_split_atomic(
    base_path: str | Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
    extrasaction: str = "ignore",
) -> list[Path]:
    path = Path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_base = _temp_split_base(path)
    try:
        temp_parts = write_rows_split(
            temp_base,
            fieldnames,
            rows,
            max_bytes=max_bytes,
            extrasaction=extrasaction,
        )
        return _replace_with_temp_parts(path, temp_base, temp_parts)
    finally:
        _cleanup_temp_parts(temp_base)


def _write_rows_single(
    path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    extrasaction: str = "ignore",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=list(fieldnames), extrasaction=extrasaction)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in fieldnames})


def trim_csv_file_to_limit(path: str | Path, *, max_bytes: int = DEFAULT_MAX_CSV_BYTES) -> bool:
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= max_bytes:
        return False
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than 0")

    with target.open("rb") as handle:
        header = handle.readline()
        header_size = len(header)
        if header_size == 0:
            return False
        if header_size > max_bytes:
            raise ValueError(
                f"CSV header is {header_size} bytes, larger than the configured limit {max_bytes}"
            )

        body_limit = max_bytes - header_size
        file_size = target.stat().st_size
        start = max(header_size, file_size - body_limit)
        handle.seek(start)
        if start > header_size:
            handle.readline()
        body = handle.read()

    while header_size + len(body) > max_bytes:
        newline_index = body.find(b"\n")
        if newline_index < 0:
            body = b""
            break
        body = body[newline_index + 1 :]

    fd, tmp_name = tempfile.mkstemp(prefix=target.stem + ".", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("wb") as handle:
            handle.write(header)
            handle.write(body)
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)
    return True


def write_rows_rolling_atomic(
    base_path: str | Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
    extrasaction: str = "ignore",
) -> list[Path]:
    path = Path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header_bytes = len(_render_csv_header(fieldnames).encode("utf-8"))
    if header_bytes > max_bytes:
        raise ValueError(
            f"CSV header is {header_bytes} bytes, larger than the configured limit {max_bytes}"
        )

    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp.csv", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _write_rows_single(tmp_path, fieldnames, rows, extrasaction=extrasaction)
        trim_csv_file_to_limit(tmp_path, max_bytes=max_bytes)
        for old_part in stale_split_parts(path):
            old_part.unlink(missing_ok=True)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return [path]


def write_dataframe_rolling_atomic(
    frame: pd.DataFrame,
    base_path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> list[Path]:
    return write_rows_rolling_atomic(
        base_path,
        list(frame.columns),
        dataframe_rows(frame),
        max_bytes=max_bytes,
    )


def ensure_rolling_csv_limit(
    base_path: str | Path,
    fieldnames: Sequence[str] | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> list[Path]:
    path = Path(base_path)
    parts = existing_csv_parts(path)
    if not parts:
        if fieldnames is None:
            return []
        return write_rows_rolling_atomic(path, fieldnames, [], max_bytes=max_bytes)
    if stale_split_parts(path):
        header = list(fieldnames or [])
        if not header:
            with parts[0].open("r", newline="", encoding="utf-8") as csvfile:
                reader = csv.reader(csvfile)
                try:
                    header = next(reader)
                except StopIteration:
                    header = []
        if not header:
            raise ValueError(f"Cannot compact {path}; CSV header is missing")
        return write_rows_rolling_atomic(path, header, iter_dict_rows(path), max_bytes=max_bytes)
    trim_csv_file_to_limit(path, max_bytes=max_bytes)
    return [path]


def append_rows_rolling(
    base_path: str | Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
    extrasaction: str = "ignore",
) -> list[Path]:
    path = Path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_rolling_csv_limit(path, fieldnames, max_bytes=max_bytes)
    mode = "a" if path.is_file() and path.stat().st_size > 0 else "w"
    with path.open(mode, newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=list(fieldnames), extrasaction=extrasaction)
        if mode == "w":
            writer.writeheader()
        for row in rows:
            row_text = _render_csv_row(fieldnames, row)
            if len(_render_csv_header(fieldnames).encode("utf-8")) + len(row_text.encode("utf-8")) > max_bytes:
                raise ValueError(
                    f"One CSV row is larger than the configured limit {max_bytes}"
                )
            writer.writerow({field: _csv_value(row.get(field, "")) for field in fieldnames})
    trim_csv_file_to_limit(path, max_bytes=max_bytes)
    remove_stale_split_parts(path)
    return [path]


def ensure_csv_size_limit(
    base_path: str | Path,
    fieldnames: Sequence[str] | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> list[Path]:
    path = Path(base_path)
    parts = existing_csv_parts(path)
    if not parts:
        if fieldnames is None:
            return []
        return write_rows_split_atomic(path, fieldnames, [], max_bytes=max_bytes)
    if all(part.stat().st_size <= max_bytes for part in parts):
        return parts

    first_part = parts[0]
    with first_part.open("r", newline="", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        try:
            header = next(reader)
        except StopIteration:
            header = list(fieldnames or [])
    if not header:
        header = list(fieldnames or [])
    if not header:
        raise ValueError(f"Cannot split {path}; CSV header is missing")

    def rows() -> Iterable[dict[str, Any]]:
        for part in parts:
            if part.stat().st_size == 0:
                continue
            with part.open("r", newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    yield row

    return write_rows_split_atomic(path, header, rows(), max_bytes=max_bytes)


def write_header_if_missing(
    base_path: str | Path,
    fieldnames: Sequence[str],
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> list[Path]:
    path = Path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if has_csv_data(path):
        return ensure_rolling_csv_limit(path, fieldnames, max_bytes=max_bytes)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as handle:
            handle.write(_render_csv_header(fieldnames))
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    remove_stale_split_parts(path)
    return [path]


