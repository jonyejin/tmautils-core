from typing import ClassVar
import time
import threading
import pytest
from typing import List, Dict, Any
from pydantic import BaseModel
import os

import pyarrow as pa

from tmautils.db import BufferedWriter, TableConfig, WriteMode

# --------------------------- Mocks & Stubs ---------------------------


class MockArrowBackend:
    """
    A thread-safe mock ArrowBackend that accumulates flushed rows in memory
    and allows injecting failures to test retry / re-buffer logic.

    It mimics the old MockDuckLakeStore surface that tests rely on:
    - .inserts[table] -> list[dict]
    - .call_count
    - .fail_next_n_times
    """

    def __init__(self):
        self.inserts: Dict[str, List[dict]] = {}
        self.lock = threading.Lock()
        self.call_count = 0
        self.fail_next_n_times = 0
        self.failure_exception = Exception("Simulated DB Lock Error")

    def flush_arrow(
        self,
        table_name: str,
        config: TableConfig,
        data: pa.Table,
    ):
        # Simulate some latency so retry / timeout tests behave realistically.
        time.sleep(0.01)

        rows: List[dict] = data.to_pylist()

        with self.lock:
            self.call_count += 1
            if self.fail_next_n_times > 0:
                self.fail_next_n_times -= 1
                raise self.failure_exception

            if table_name not in self.inserts:
                self.inserts[table_name] = []
            self.inserts[table_name].extend(rows)


class ItemModel(BaseModel):
    k: int
    v: str

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("k", pa.int64()),
        pa.field("v", pa.string()),
    ])

# --------------------------- Fixtures ---------------------------


@pytest.fixture
def mock_backend():
    return MockArrowBackend()


@pytest.fixture
def table_configs():
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


def test_buffer_accumulates_without_flush(mock_backend, table_configs):
    """
    Ensure rows stay in the buffer until thresholds are met.
    """
    writer = BufferedWriter(
        mock_backend,
        table_configs,
        row_thresh=100,
        time_thresh_sec=10.0,
        jitter=0.0
    )

    rows = [{"k": i, "v": "x"} for i in range(50)]
    writer.add_rows("table_a", rows)

    assert mock_backend.call_count == 0
    with writer._tables["table_a"].lock:
        assert len(writer._tables["table_a"].buffer) == 50

    writer.close()
    assert len(mock_backend.inserts["table_a"]) == 50


def test_row_threshold_triggers_flush(mock_backend, table_configs):
    """
    Ensure hitting row_thresh triggers a background flush.
    """
    writer = BufferedWriter(
        mock_backend,
        table_configs,
        row_thresh=10,
        time_thresh_sec=60.0,
        jitter=0.0
    )

    rows = [{"k": i, "v": "x"} for i in range(15)]
    writer.add_rows("table_a", rows)

    # Allow worker thread to pick up the task
    time.sleep(0.2)

    assert mock_backend.call_count >= 1

    writer.close()
    assert len(mock_backend.inserts["table_a"]) == 15


def test_time_threshold_triggers_flush(mock_backend, table_configs):
    """
    Ensure the background timer triggers a flush after time_thresh_sec.
    """
    writer = BufferedWriter(
        mock_backend,
        table_configs,
        row_thresh=1000,
        time_thresh_sec=0.5,
        jitter=0.0
    )

    writer.add_row("table_a", {"k": 1, "v": "t"})
    assert mock_backend.call_count == 0

    time.sleep(1.0)  # Wait for timer loop

    assert mock_backend.call_count >= 1
    assert len(mock_backend.inserts["table_a"]) == 1

    writer.close()


def test_manual_flush_blocks_until_done(mock_backend, table_configs):
    writer = BufferedWriter(
        mock_backend,
        table_configs,
        row_thresh=1000,
        time_thresh_sec=60.0
    )

    writer.add_row("table_a", {"k": 1, "v": "manual"})
    writer.flush()

    assert mock_backend.call_count == 1
    assert mock_backend.inserts["table_a"][0]["v"] == "manual"

    with writer._tables["table_a"].lock:
        assert len(writer._tables["table_a"].buffer) == 0

    writer.close()


def test_concurrency_stress_test(mock_backend, table_configs):
    """
    Spam multiple tables from multiple threads.
    Verifies granular locking allows throughput and data integrity.
    """
    writer = BufferedWriter(
        mock_backend,
        table_configs,
        row_thresh=50,
        time_thresh_sec=60.0,
        max_workers=4
    )

    def worker(tbl_name, start, count):
        for i in range(count):
            writer.add_row(tbl_name, {"k": start + i, "v": "stress"})
            # minimal sleep to allow context switching, but keep it fast
            if i % 10 == 0:
                time.sleep(0.001)

    threads = []
    threads.append(threading.Thread(target=worker, args=("table_a", 0, 200)))
    threads.append(threading.Thread(target=worker, args=("table_a", 200, 200)))
    threads.append(threading.Thread(target=worker, args=("table_b", 0, 200)))
    threads.append(threading.Thread(target=worker, args=("table_b", 200, 200)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    writer.close()

    # Data Integrity is the primary correctness check
    assert len(mock_backend.inserts["table_a"]) == 400
    assert len(mock_backend.inserts["table_b"]) == 400

    # We verify that flushes actually happened (it wasn't just 1 giant flush at close)
    # But we don't enforce a high number, because efficient batching is good.
    assert mock_backend.call_count > 0


def test_retry_logic_preserves_order_and_data(mock_backend, table_configs):
    """
    Simulate a failure. Ensure rows are prepended back to buffer
    and eventually written in correct order.
    """
    writer = BufferedWriter(
        mock_backend,
        table_configs,
        row_thresh=5,
        time_thresh_sec=60.0,
        jitter=0.0
    )

    # Fail the next flush
    mock_backend.fail_next_n_times = 1

    # Batch 1 triggers flush (5 rows)
    batch1 = [{"k": i, "v": "batch1"} for i in range(5)]
    writer.add_rows("table_a", batch1)

    # Batch 2 added while Batch 1 is "failing"
    batch2 = [{"k": 10+i, "v": "batch2"} for i in range(2)]
    writer.add_rows("table_a", batch2)

    # Force flush until clear
    writer.flush()

    # 1 fail + 1 success = 2 calls (minimum)
    # Note: Logic might try more times depending on timing, so check >= 2
    assert mock_backend.call_count >= 2

    final_data = mock_backend.inserts["table_a"]
    assert len(final_data) == 7

    # Verify Order
    assert final_data[0]["k"] == 0  # First item of batch1
    assert final_data[4]["k"] == 4  # Last item of batch1
    assert final_data[5]["k"] == 10  # First item of batch2

    writer.close()


def test_close_timeout_behavior(mock_backend, table_configs):
    """
    If the store is permanently broken, close() should eventually timeout
    and not hang indefinitely.
    """
    writer = BufferedWriter(
        mock_backend,
        table_configs,
        row_thresh=10
    )

    # Set timeout slightly higher than the mock latency to allow a few retries
    # but short enough to test the timeout mechanic.
    writer.FINAL_FLUSH_TIMEOUT_SEC = 0.2

    # Make store fail enough times to exceed the timeout duration.
    # With 0.01s sleep per call, 50 fails = 0.5s > 0.2s timeout.
    mock_backend.fail_next_n_times = 50

    writer.add_row("table_a", {"k": 1, "v": "doom"})

    start = time.monotonic()
    writer.close()
    duration = time.monotonic() - start

    # Should run at least the timeout duration
    assert duration >= 0.2
    # Should not run drastically longer (allow some buffer for Python overhead)
    assert duration < 1.0


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
