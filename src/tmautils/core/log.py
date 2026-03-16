# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Any, Dict, Optional
from pathlib import Path
from dataclasses import dataclass, field
from enum import StrEnum
import logging
from logging.handlers import QueueHandler, QueueListener
import multiprocessing as mp


class _CtxLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, ctx_str: str):
        super().__init__(logger, {"ctx": ctx_str})

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("ctx", self.extra.get("ctx", ""))
        return msg, kwargs


class _EnsureLogCtx(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "ctx"):
            record.ctx = ""
        return True


def _setup_log_ctx(
    logger: logging.Logger,
    *,
    sort_keys: bool = True,
    **extra
) -> logging.LoggerAdapter:
    items = sorted(extra.items()) if sort_keys else list(extra.items())
    ctx_str = (" " + " ".join(f"{k}={v}" for k, v in items)) if items else ""
    return _CtxLoggerAdapter(logger, ctx_str)


def _attach_ctx_filter(h: logging.Handler):
    if not any(isinstance(f, _EnsureLogCtx) for f in h.filters):
        h.addFilter(_EnsureLogCtx())
    return h


class _NoopLogger:
    def __getattr__(self, _name: str):
        def _noop(*_a: Any, **_k: Any):
            pass
        return _noop


_NOOP_LOGGER = _NoopLogger()


class LogRotationMode(StrEnum):
    NONE = "none"
    TIME = "time"
    SIZE = "size"


@dataclass(slots=True)
class LogConfig:
    """
    Configuration for logging setup.

    Usage:
    - Parent/main process:
        - set `is_worker=False` (default)
        - if logging to file is desired, set `log_path` to a file path
        - if logging to file is not desired, set `file_level=None`
    - Worker process:
        - set `is_worker=True`
        - set `log_queue` to the multiprocessing.Queue created by the parent

    Args:
        name (str):
            Name of the logger.

        is_worker (bool):
            If True, set up logging in worker mode (using QueueHandler).
            If False, set up logging in listener mode (using QueueListener).
            Default is False.

        log_path (Optional[Path]):
            Path to the log file (used in listener mode).
            Required if `is_worker` is False and file logging is enabled.

        queue_maxsize (int):
            Maximum size of the log queue to be created (used in listener mode).
            Default is 10,000.

        log_queue (Optional[mp.Queue]):
            Multiprocessing queue for log records (used in worker mode).
            Required if `is_worker` is True.

        console_level (Optional[int]):
            Logging level for console output.
            Set to None to disable console logging.
            Default is logging.WARNING.

        file_level (Optional[int]):
            Logging level for file output.
            Set to None to disable file logging.
            Default is logging.INFO.

        utc (bool):
            If True, timestamps in log entries will be in UTC.
            If False, local time will be used.
            Default is True.

        rotation (LogRotationMode):
            Rotation mode for log files.
            Default is LogRotationMode.SIZE.

        rotate_backup (int):
            Number of backup files to keep.
            Default is 5.

        rotate_when (str):
            Specifies the type of interval for time-based rollover.
            See the `TimedRotatingFileHandler` documentation for options.
            Default is "D" (daily).

        rotate_interval (int):
            Interval for time-based rollover.
            Default is 1.

        rotate_max_bytes (int):
            If rotation mode is SIZE, enables size-based rollover when the log file
            reaches this size in bytes.
            Default is 100 MiB.

        ctx_kwargs (Dict[str, Any]):
            Additional context to add to log records.
            For example, `{'worker_id': 'A'}`
            Default is empty dict.

        register_atexit (bool):
            If True, the log listener will be shut down automatically on program exit.
            Default is True.
    """
    # Identity
    name: str

    # Process role
    is_worker: bool = False

    # Targets
    log_path: Optional[Path] = None  # used if is_worker=False
    queue_maxsize: int = 10_000  # used if is_worker=False
    log_queue: Optional[mp.Queue] = None  # used if is_worker=True

    # Levels (set to None to disable)
    console_level: Optional[int] = logging.WARNING
    file_level: Optional[int] = logging.INFO

    # Time & rotation
    utc: bool = True
    rotation: LogRotationMode = LogRotationMode.SIZE
    rotate_backup: int = 5
    rotate_when: str = "D"  # used if time-based rotation
    rotate_interval: int = 1  # used if time-based rotation
    rotate_max_bytes: int = 100 * (2**20)  # 100 MiB; for size-based rotation

    # Additional log context
    ctx_kwargs: Dict[str, Any] = field(default_factory=dict)

    # Misc
    register_atexit: bool = True


class LogHelper:
    """
    LogHelper sets up logging for both listener (main) and worker processes.
    In listener mode, it sets up logging to console and file with a QueueListener.
    In worker mode, it sets up a QueueHandler to send log records to the listener.

    Args:
        config (LogConfig):
            Configuration for logging setup.
    """

    _FORMAT_STR_BASE = (
        "%(asctime)s.%(msecs)03d{} %(levelname)s "
        "[unit=%(name)s pid=%(process)d thread=%(threadName)s%(ctx)s] "
        "(%(funcName)s@%(module)s:%(lineno)d) %(message)s"
    )
    UTC_FORMAT_STR = _FORMAT_STR_BASE.format("Z")
    LOCALTIME_FORMAT_STR = _FORMAT_STR_BASE.format("")
    UTC_DATEFMT = "%Y-%m-%dT%H:%M:%S"
    LOCALTIME_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"
    LOG_LISTENER_STOP_TIMEOUT_SEC = 2.0

    def __init__(
        self,
        config: LogConfig,
    ):
        self._config = config

        self._q: Optional[mp.Queue] = None
        self._q_listener: Optional[QueueListener] = None
        self._handlers: list[logging.Handler] = []
        self._logger: Optional[logging.LoggerAdapter] = None

        if config.is_worker:
            self._setup_logging_worker()
        else:
            self._setup_logging_listener()

        self.logger.info(
            f"LogHelper for '{config.name}' set up in "
            f"{'worker' if config.is_worker else 'listener'} mode "
            f"with config: {config}"
        )

    def _make_queue_logger(self, log_queue: mp.Queue):
        config = self._config

        if config.is_worker:
            config.ctx_kwargs['role'] = 'worker'

        # Logger that sends to the queue
        logger = logging.getLogger(config.name)

        # Logger level is the minimum of console and file levels
        if config.console_level is not None and config.file_level is not None:
            level = min(config.console_level, config.file_level)
        elif config.console_level is not None:
            level = config.console_level
        elif config.file_level is not None:
            level = config.file_level
        else:
            level = logging.NOTSET
        logger.setLevel(level)

        logger.handlers[:] = [QueueHandler(log_queue)]
        logger.propagate = False

        return _setup_log_ctx(logger, **config.ctx_kwargs)

    def _setup_logging_worker(self):
        # We are in a worker process => QueueHandler sends logs to parent
        config = self._config
        if config.log_queue is None:
            raise RuntimeError("log_queue must be provided in worker mode.")
        self._logger = self._make_queue_logger(config.log_queue)

    def _get_formatter(self):
        import time

        config = self._config
        if config.utc:
            class UtcFormatter(logging.Formatter):
                converter = time.gmtime

            formatter = UtcFormatter(
                self.UTC_FORMAT_STR, datefmt=self.UTC_DATEFMT
            )
        else:
            formatter = logging.Formatter(
                self.LOCALTIME_FORMAT_STR, datefmt=self.LOCALTIME_DATEFMT
            )
        return formatter

    def _make_stream_handler(self):
        config = self._config
        h = logging.StreamHandler()
        h.setLevel(config.console_level)
        h.setFormatter(self._get_formatter())
        _attach_ctx_filter(h)
        return h

    def _make_file_handler(self):
        from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler

        config = self._config
        if config.log_path is None:
            raise RuntimeError("log_path must be provided for file handler.")

        if config.rotation == LogRotationMode.SIZE:
            if (config.rotate_max_bytes or 0) <= 0:
                raise ValueError(
                    "LogRotationMode.SIZE requires rotate_max_bytes > 0."
                )
            h = RotatingFileHandler(
                str(config.log_path),
                maxBytes=config.rotate_max_bytes,
                backupCount=config.rotate_backup,
            )
        elif config.rotation == LogRotationMode.TIME:
            h = TimedRotatingFileHandler(
                str(config.log_path),
                when=config.rotate_when,
                interval=config.rotate_interval,
                backupCount=config.rotate_backup,
                utc=config.utc,
            )
        else:  # LogRotationMode.NONE
            h = logging.FileHandler(str(config.log_path))

        h.setLevel(config.file_level)
        h.setFormatter(self._get_formatter())
        _attach_ctx_filter(h)
        return h

    def _setup_logging_listener(self):
        # We are in the main process => Set up logging to console + file with QueueListener
        config = self._config

        # Set up console and file handlers
        if config.console_level is not None:
            self._handlers.append(self._make_stream_handler())
        if config.file_level is not None:
            self._handlers.append(self._make_file_handler())

        # Queue + QueueListener + Logger that sends to the queue
        ctx = mp.get_context("spawn")
        self._q = ctx.Queue()
        self._q_listener = QueueListener(
            self._q, *self._handlers, respect_handler_level=True
        )
        self._q_listener.start()
        self._logger = self._make_queue_logger(self._q)

        # Shutdown listener on exit
        if config.register_atexit:
            import atexit
            atexit.register(self._shutdown_logging)

    def _shutdown_logging(self):
        if self._q_listener is None:
            return
        self.logger.info("Shutting down logging...")

        lst = self._q_listener
        self._q_listener = None

        # Stop the listener subject to a timeout
        try:
            import threading
            stopper = threading.Thread(target=lst.stop, daemon=True)
            stopper.start()
            stopper.join(self.LOG_LISTENER_STOP_TIMEOUT_SEC)
        except Exception:
            pass

        # Close handlers
        if self._handlers:
            for h in self._handlers:
                try:
                    h.flush()
                    h.close()
                except Exception:
                    pass
            self._handlers = []

        self._q = None
        self._logger = None

    @property
    def logger(self) -> logging.LoggerAdapter:
        """
        Get the logger for this LogHelper.
        If no logger is set up, returns a no-op logger.
        """
        if self._logger is None:
            # No logger set up => return no-op logger so caller is happy
            return _NOOP_LOGGER
        return self._logger

    @property
    def log_queue(self):
        """
        Get the log queue if running in listener mode.
        This is useful while spawning worker processes that need to send
        log records to the parent process.
        """
        if self._config.is_worker:
            raise RuntimeError("Log queue is not available in worker mode.")
        if self._q is None:
            raise RuntimeError("Log queue is not set up.")
        return self._q

    def get_worker_config(self) -> LogConfig:
        """
        Convienience method to get a LogConfig for worker processes.
        This can be used while spawning worker processes to set up logging in worker mode.
        """
        config = self._config
        worker_config = LogConfig(
            name=config.name,
            is_worker=True,
            log_path=None,
            log_queue=self.log_queue,
            console_level=config.console_level,
            file_level=config.file_level,
            utc=config.utc,
            ctx_kwargs={**config.ctx_kwargs, "role": "worker"},
        )
        return worker_config


def get_logger_from_helper(log_helper: Optional[LogHelper]) -> logging.LoggerAdapter:
    """
    Given an optional LogHelper, return its logger if present,
    else return a no-op logger.
    """
    return log_helper.logger if log_helper is not None else _NOOP_LOGGER
