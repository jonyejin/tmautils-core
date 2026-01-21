# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Dict, List, Optional
import threading
import time
import random
from concurrent.futures import ThreadPoolExecutor, wait

from .base import ArrowBackend, TableConfig, pydantic_to_arrow
from tmautils.common import LogHelper, get_logger_from_helper


class _TableBuffer:
    def __init__(self, name: str, config: TableConfig):
        self.name = name
        self.config = config

        self.lock = threading.Lock()
        self.buffer: List[dict] = []
        self.last_flush_ts: float = time.monotonic()
        self.is_flushing: bool = False


class BufferedWriter:
    """
    Buffered writer that batches rows and flushes them to a backend
    implementing the ArrowBackend protocol.
    Rows are buffered per-table according to the provided TableConfig.
    Flushes are triggered when either a row count threshold or
    time threshold is exceeded.

    Args:
        backend (ArrowBackend):
            The backend to which data will be flushed.
            See DuckLakeBackend and ParquetBackend for example implementations.

        table_configs (Dict[str, TableConfig]):
            A mapping of table names to their corresponding TableConfig.

        auto_create_tables (bool):
            Whether to automatically create SQL tables from model metadata
            when the backend supports it (e.g., DuckDbBackend, DuckLakeBackend).
            Uses CREATE TABLE IF NOT EXISTS, so it's safe to use with existing tables.
            Default is True.
            Set to False for manual table creation control.

        row_thresh (int):
            The number of buffered rows per table that triggers a flush.
            Default is 10,000.

        time_thresh_sec (float):
            The number of seconds since the last flush per table that triggers a flush.
            Default is 300.0 (5 minutes).

        jitter (float):
            A jitter factor (0.0 to 1.0) to apply to the row and time thresholds
            to avoid thundering herd flushes. Default is 0.2 (20%).

        max_workers (int):
            The maximum number of worker threads for flushing data.
            Default is 1.

        log_helper (Optional[LogHelper]):
            Optional LogHelper for logging.
            If not provided, no logging will be performed.
    """

    DEFAULT_ROW_THRESH = 10_000
    DEFAULT_TIME_THRESH_SEC = 300.0  # 5 minutes
    DEFAULT_JITTER = 0.2
    FINAL_FLUSH_TIMEOUT_SEC = 15.0

    def __init__(
        self,
        backend: ArrowBackend,
        table_configs: Dict[str, TableConfig],
        *,
        auto_create_tables: bool = True,
        row_thresh: int = DEFAULT_ROW_THRESH,
        time_thresh_sec: float = DEFAULT_TIME_THRESH_SEC,
        jitter: float = DEFAULT_JITTER,
        max_workers: int = 1,
        log_helper: Optional[LogHelper] = None,
    ) -> None:
        self._backend = backend
        self._logger = get_logger_from_helper(log_helper)

        self._base_row_thresh = row_thresh
        self._base_time_thresh = time_thresh_sec
        self._jitter = jitter

        # Initialize per-table buffers
        self._tables: Dict[str, _TableBuffer] = {
            name: _TableBuffer(name, cfg)
            for name, cfg in table_configs.items()
        }

        # Auto-create tables if backend supports it
        if auto_create_tables and hasattr(backend, 'ensure_table'):
            for table_name, config in table_configs.items():
                try:
                    backend.ensure_table(table_name, config)
                    self._logger.debug(
                        "Auto-created table '%s' from model %s.",
                        table_name,
                        config.model.__name__
                    )
                except Exception as e:
                    self._logger.warning(
                        "Failed to auto-create table '%s': %s. "
                        "Table may already exist or require manual creation.",
                        table_name, e
                    )

        # Thread pool for flush tasks
        self._exec = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"{self.__class__.__name__}-worker",
        )
        self._closed = False

        # Timer thread for time-based flushes
        self._timer_thread = threading.Thread(
            target=self._timer_loop,
            name=f"{self.__class__.__name__}-timer",
            daemon=True,
        )
        self._timer_thread.start()

        self._logger.info(
            "%s initialized with backend type %s, "
            "row_thresh=%d, time_thresh=%.1f, max_workers=%d.",
            self.__class__.__name__,
            type(backend).__name__,
            self._base_row_thresh,
            self._base_time_thresh,
            max_workers,
        )

    def _get_thresholds_dynamic(self) -> tuple[int, float]:
        if self._jitter <= 0:
            return self._base_row_thresh, self._base_time_thresh

        mult = random.uniform(1.0 - self._jitter, 1.0 + self._jitter)
        return int(self._base_row_thresh * mult), self._base_time_thresh * mult

    def _timer_loop(self):
        # Check more frequently than the threshold to catch timeouts accurately
        interval = max(
            0.1,
            min(self._base_time_thresh / 4, self.FINAL_FLUSH_TIMEOUT_SEC / 2)
        )

        while not self._closed:
            time.sleep(interval)
            if self._closed:
                break

            try:
                self._check_time_thresholds()
            except Exception:
                self._logger.error(
                    "BufferedWriter timer-loop flush failed.",
                    exc_info=True,
                )

    def _check_time_thresholds(self):
        _, time_thresh = self._get_thresholds_dynamic()
        now = time.monotonic()

        for tb in self._tables.values():
            # Check without lock first.
            # Dirty read is acceptable here because
            # we either double-check inside the lock, or will try again later.
            if tb.is_flushing or not tb.buffer:
                continue

            should_flush = False
            rows_to_flush: List[dict] = []

            with tb.lock:
                if not tb.is_flushing and tb.buffer:
                    if (now - tb.last_flush_ts) >= time_thresh:
                        should_flush = True
                        tb.is_flushing = True
                        rows_to_flush = tb.buffer
                        tb.buffer = []

            if should_flush and rows_to_flush:
                self._exec.submit(self._flush_task, tb, rows_to_flush, now)

    def add_row(self, table: str, row: dict):
        """
        Add a single row to the buffer for the specified table.
        """

        self.add_rows(table, [row])

    def add_rows(self, table: str, rows: List[dict]):
        """
        Add rows to the buffer for the specified table.
        """

        if self._closed:
            raise RuntimeError("BufferedWriter is closed; cannot add rows.")
        if not rows:
            return

        tb = self._tables.get(table)
        if not tb:
            raise KeyError(f"Unknown table '{table}'")

        row_thresh, _ = self._get_thresholds_dynamic()
        should_flush = False
        rows_to_flush: Optional[List[dict]] = None

        with tb.lock:
            tb.buffer.extend(rows)

            if not tb.is_flushing and len(tb.buffer) >= row_thresh:
                should_flush = True
                tb.is_flushing = True
                rows_to_flush = tb.buffer
                tb.buffer = []

        if should_flush and rows_to_flush:
            self._exec.submit(
                self._flush_task, tb, rows_to_flush, time.monotonic()
            )

    def _flush_task(self, tb: _TableBuffer, rows: List[dict], start_ts: float):
        if not rows:
            with tb.lock:
                tb.is_flushing = False
            return

        try:
            instances = [tb.config.model(**r) for r in rows]
            table = pydantic_to_arrow(instances, tb.config.model)
            if table is None or table.num_rows == 0:
                with tb.lock:
                    tb.is_flushing = False
                return

            self._backend.flush_arrow(tb.name, tb.config, table)

            with tb.lock:
                tb.last_flush_ts = start_ts
                tb.is_flushing = False

            self._logger.debug(
                "BufferedWriter: flushed %d rows to table '%s'.",
                len(rows), tb.name,
            )

        except Exception as e:
            self._logger.error(
                "BufferedWriter: flush for table '%s' failed: %s. Re-buffering rows.",
                tb.name, e, exc_info=True,
            )
            with tb.lock:
                tb.buffer = rows + tb.buffer
                tb.is_flushing = False

    def flush(self, timeout: Optional[float] = None):
        """
        Force flushes all tables and waits for completion.

        Iterates until all buffers are empty and no flushes are in-flight,
        or timeout is reached.
        """

        start_time = time.monotonic()

        while True:
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed > timeout:
                    self._logger.warning(
                        "Flush timed out with data remaining."
                    )
                    break
                remaining_time = timeout - elapsed
            else:
                remaining_time = None

            pending_count = 0
            futures = []

            for tb in self._tables.values():
                with tb.lock:
                    # Buffer has data and not flushing => schedule flush
                    if tb.buffer and not tb.is_flushing:
                        tb.is_flushing = True
                        data = tb.buffer
                        tb.buffer = []

                        f = self._exec.submit(
                            self._flush_task, tb, data, time.monotonic()
                        )
                        futures.append(f)
                        pending_count += 1

                    # Currently flushing, regardless of buffer state => pending
                    elif tb.is_flushing:
                        pending_count += 1

            if pending_count == 0:
                break

            if futures:
                # We scheduled some flushes; wait for them.
                wait(futures, timeout=remaining_time)
            else:
                # Someone else is flushing; give them some time.
                time.sleep(0.1)

    def close(self):
        """
        Close the BufferedWriter, flushing any remaining data.
        """

        if self._closed:
            return
        self._closed = True

        try:
            self.flush(timeout=self.FINAL_FLUSH_TIMEOUT_SEC)
        except Exception:
            self._logger.error(
                "Final BufferedWriter flush failed.", exc_info=True
            )

        self._exec.shutdown(wait=True)

    def __enter__(self) -> "BufferedWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
