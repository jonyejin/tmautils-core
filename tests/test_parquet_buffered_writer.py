import os
import time
import threading
from typing import List, Dict, ClassVar

import duckdb
import pyarrow as pa
import pytest
from pydantic import BaseModel
from pathlib import Path

from tmautils.db import (
    BufferedWriter,
    TableConfig,
    ParquetBackend,
    WriteMode,
)


# --------------------------- Models ---------------------------


class ItemModel(BaseModel):
    k: int
    v: str

    # Match the production contract: models must define ARROW_SCHEMA
    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("k", pa.int64()),
        pa.field("v", pa.string()),
    ])


# --------------------------- Helpers ---------------------------


def _count_rows_in_table_dir(base_path: Path, table_name: str) -> int:
    """
    Count all rows across all parquet files in base_path / table_name.
    Uses DuckDB's read_parquet.
    """
    table_dir = base_path / table_name
    pattern = str(table_dir / "*.parquet")

    if not table_dir.exists():
        return 0

    con = duckdb.connect()
    try:
        # DuckDB supports glob patterns in read_parquet
        result = con.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [pattern],
        ).fetchone()
        return int(result[0]) if result else 0
    finally:
        con.close()


def _list_parquet_files(base_path: Path, table_name: str) -> List[Path]:
    table_dir = base_path / table_name
    if not table_dir.exists():
        return []
    return list(table_dir.glob("*.parquet"))


# --------------------------- Fixtures ---------------------------


@pytest.fixture
def base_path(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def parquet_backend(base_path: Path) -> ParquetBackend:
    # Let backend create its own in-memory DuckDB connection
    return ParquetBackend(base_path=base_path)


@pytest.fixture
def table_configs() -> Dict[str, TableConfig]:
    return {
        "table_a": TableConfig(
            model=ItemModel,
            key_cols=["k"],
            mode=WriteMode.APPEND,
        ),
        "table_b": TableConfig(
            model=ItemModel,
            key_cols=["k"],
            mode=WriteMode.APPEND,
        ),
    }


# --------------------------- Tests ---------------------------


def test_buffer_accumulates_without_flush(parquet_backend, table_configs, base_path):
    """
    Ensure rows stay in the buffer until thresholds are met, and are only
    written to parquet on close().
    """
    writer = BufferedWriter(
        backend=parquet_backend,
        table_configs=table_configs,
        row_thresh=100,
        time_thresh_sec=10.0,
        jitter=0.0,
    )

    rows = [{"k": i, "v": "x"} for i in range(50)]
    writer.add_rows("table_a", rows)

    # No parquet files yet; nothing has hit thresholds
    assert _count_rows_in_table_dir(base_path, "table_a") == 0
    assert _list_parquet_files(base_path, "table_a") == []

    # Check buffer contents directly (like the DuckLake tests)
    with writer._tables["table_a"].lock:
        assert len(writer._tables["table_a"].buffer) == 50

    writer.close()

    # Now everything should have been flushed to parquet
    assert _count_rows_in_table_dir(base_path, "table_a") == 50
    assert len(_list_parquet_files(base_path, "table_a")) >= 1


def test_row_threshold_triggers_flush(parquet_backend, table_configs, base_path):
    """
    Ensure hitting row_thresh triggers a background flush that writes parquet.
    """
    writer = BufferedWriter(
        backend=parquet_backend,
        table_configs=table_configs,
        row_thresh=10,
        time_thresh_sec=60.0,
        jitter=0.0,
    )

    rows = [{"k": i, "v": "x"} for i in range(15)]
    writer.add_rows("table_a", rows)

    # Allow worker thread to pick up the task
    time.sleep(0.3)

    # We expect at least one flush before close
    assert len(_list_parquet_files(base_path, "table_a")) >= 1

    writer.close()
    # All rows should be present across all parquet files
    assert _count_rows_in_table_dir(base_path, "table_a") == 15


def test_time_threshold_triggers_flush(parquet_backend, table_configs, base_path):
    """
    Ensure the background timer triggers a flush after time_thresh_sec, even if
    row_thresh is not hit.
    """
    writer = BufferedWriter(
        backend=parquet_backend,
        table_configs=table_configs,
        row_thresh=1000,
        time_thresh_sec=0.5,
        jitter=0.0,
    )

    writer.add_row("table_a", {"k": 1, "v": "t"})
    assert _count_rows_in_table_dir(base_path, "table_a") == 0

    time.sleep(1.0)  # Wait for timer loop

    # Timer-based flush should have written at least one parquet file
    assert len(_list_parquet_files(base_path, "table_a")) >= 1
    assert _count_rows_in_table_dir(base_path, "table_a") == 1

    writer.close()


def test_manual_flush_blocks_until_done(parquet_backend, table_configs, base_path):
    """
    flush() should block until all pending buffers have been written to parquet.
    """
    writer = BufferedWriter(
        backend=parquet_backend,
        table_configs=table_configs,
        row_thresh=1000,
        time_thresh_sec=60.0,
    )

    writer.add_row("table_a", {"k": 1, "v": "manual"})
    writer.flush()

    assert _count_rows_in_table_dir(base_path, "table_a") == 1

    # Buffer should be empty for table_a
    with writer._tables["table_a"].lock:
        assert len(writer._tables["table_a"].buffer) == 0

    writer.close()


def test_concurrency_stress_test(parquet_backend, table_configs, base_path):
    """
    Spam multiple tables from multiple threads.
    Verifies that parquet files are written and data is complete.
    """
    writer = BufferedWriter(
        backend=parquet_backend,
        table_configs=table_configs,
        row_thresh=50,
        time_thresh_sec=60.0,
        max_workers=4,
    )

    def worker(tbl_name: str, start: int, count: int):
        for i in range(count):
            writer.add_row(tbl_name, {"k": start + i, "v": "stress"})
            if i % 10 == 0:
                time.sleep(0.001)

    threads = [
        threading.Thread(target=worker, args=("table_a", 0, 200)),
        threading.Thread(target=worker, args=("table_a", 200, 200)),
        threading.Thread(target=worker, args=("table_b", 0, 200)),
        threading.Thread(target=worker, args=("table_b", 200, 200)),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    writer.close()

    # Data integrity: all rows must be present
    assert _count_rows_in_table_dir(base_path, "table_a") == 400
    assert _count_rows_in_table_dir(base_path, "table_b") == 400

    # There should be at least one parquet file per table
    assert len(_list_parquet_files(base_path, "table_a")) > 0
    assert len(_list_parquet_files(base_path, "table_b")) > 0


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
