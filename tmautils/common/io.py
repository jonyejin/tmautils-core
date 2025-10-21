from pathlib import Path
import logging
from logging.handlers import QueueListener, QueueHandler
import multiprocessing as mp

from .types import *

_GZIP_STREAM_CHUNK_SIZE = 64 * 1024  # 64 KiB


def import_module_attr(module_path: str, attr_name: str):
    """
    Import an attribute from a module.
    Useful for dynamically importing functions or classes.
    See TrancoCrawlUtil for an example.

    Args:
        module_path (str):
            Path to the module to import from.
            This should be a string in the format 'package.module'.

        attr_name (str):
            Name of the attribute to import from the module.

    Returns:
        attr (object):
            The imported attribute (function, class, etc.) from the module.
    """

    import importlib

    try:
        module = importlib.import_module(module_path)
        attr = getattr(module, attr_name)
    except ModuleNotFoundError as e:
        raise ImportError(
            f"Module '{module_path}' not found."
        ) from e
    except AttributeError as e:
        raise ImportError(
            f"Attribute '{attr_name}' not found in module '{module_path}'"
        ) from e
    return attr


def gzip_file(
    file_path: Path,
    force: bool = False,
    delete_original: bool = True,
    compression_level: int = 9,
    logger: logging.Logger | None = None,
):
    """
    Compress a file using gzip.

    Args:
        file_path (Path):
            Path to the file to be compressed.

        force (bool):
            If True, will overwrite the existing gzipped file if it exists.
            If False, will skip compression if the gzipped file already exists.
            Default is False.

        delete_original (bool):
            If True, the original file will be deleted after compression.
            If False, the original file will be kept.
            Default is True.

        compression_level (int):
            Compression level for gzip, from 0 (no compression) to 9 (maximum compression).
            Default is 9 (maximum compression).

        logger (logging.Logger | None):
            Optional logger to log messages.
            If None, no logging will be performed.

    Returns:
        gz_path (Path):
            Path to the compressed gzipped file.
    """
    gz_path = file_path.with_suffix(f"{file_path.suffix}.gz")

    if gz_path.exists():
        if not force:
            if logger is not None:
                logger.info(f"{gz_path} already exists. Skipping compression.")
            if delete_original:
                file_path.unlink(missing_ok=True)
            return gz_path
        else:
            gz_path.unlink(missing_ok=True)

    tmp_gz = gz_path.with_name(f"{gz_path.name}.tmp")
    try:
        import gzip
        import shutil
        import os

        with (
            file_path.open("rb") as f_in,
            gzip.open(tmp_gz, "wb", compresslevel=compression_level) as f_out
        ):
            # Stream in chunks
            shutil.copyfileobj(f_in, f_out, length=_GZIP_STREAM_CHUNK_SIZE)

        os.replace(tmp_gz, gz_path)
        if delete_original:
            file_path.unlink()

        if logger is not None:
            logger.info(f"Compressed {file_path} to {gz_path}")

        return gz_path
    except Exception as e:
        if logger is not None:
            logger.error(f"Failed to gzip {file_path}: {e}")
        if tmp_gz.exists():
            tmp_gz.unlink()
        raise


def gunzip_file(
    gz_path: Path,
    force: bool = False,
    delete_gzip: bool = True,
    logger: logging.Logger | None = None,
):
    """
    Decompress a gzipped file.

    Args:
        gz_path (Path):
            Path to the gzipped file to be decompressed.

        force (bool):
            If True, will overwrite the existing unzipped file if it exists.
            If False, will skip decompression if the unzipped file already exists.
            Default is False.

        delete_gzip (bool):
            If True, the gzipped file will be deleted after decompression.
            If False, the gzipped file will be kept.
            Default is True.

        logger (logging.Logger | None):
            Optional logger to log messages.
            If None, no logging will be performed.

    Returns:
        unzipped_path (Path):
            Path to the decompressed file.
    """

    unzipped_path = gz_path.with_suffix("")

    if unzipped_path.exists():
        if not force:
            if logger is not None:
                logger.info(
                    f"{unzipped_path} already exists. Skipping decompression."
                )
            if delete_gzip:
                gz_path.unlink(missing_ok=True)
            return unzipped_path
        else:
            unzipped_path.unlink(missing_ok=True)

    tmp_unzipped = unzipped_path.with_name(f"{unzipped_path.name}.tmp")
    try:
        import gzip
        import shutil
        import os

        with gzip.open(gz_path, "rb") as f_in, tmp_unzipped.open("wb") as f_out:
            # Stream in chunks
            shutil.copyfileobj(f_in, f_out, length=_GZIP_STREAM_CHUNK_SIZE)

        os.replace(tmp_unzipped, unzipped_path)
        if delete_gzip:
            gz_path.unlink()

        if logger is not None:
            logger.info(f"Decompressed {gz_path} to {unzipped_path}")

        return unzipped_path
    except Exception as e:
        if logger is not None:
            logger.error(f"Failed to gunzip {gz_path}: {e}")
        if tmp_unzipped.exists():
            tmp_unzipped.unlink()
        raise


class IOHelper:
    """
    IOHelper is a utility class for managing input/output operations in a structured way.

    Args:
        namespace (str):
            Name of the namespace for this unit.
            It becomes a directory under `working_root` (i.e., `working_root/namespace/`).

            If `instance_name` is given, the structure is `working_root/namespace/instance_name/{raw,processed,logs,results}`.

            Otherwise, the `{raw,processed,logs,results}` directories are directly under `working_root/namespace/`.

        instance_name (str | None):
            Optional instance name.
            If provided, it will be used to create a subdirectory under the namespace directory.

        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        raw_dir_symlink_to (Path | None):
            Path to which the raw directory should be symlinked.
            If provided, the `raw` directory will be a symlink to this path.
            If None, the `raw` directory will be created in the namespace directory.

        processed_dir_symlink_to (Path | None):
            Path to which the processed directory should be symlinked.
            If provided, the `processed` directory will be a symlink to this path.
            If None, the `processed` directory will be created in the namespace directory.

        logs_dir_symlink_to (Path | None):
            Path to which the logs directory should be symlinked.
            If provided, the `logs` directory will be a symlink to this path.
            If None, the `logs` directory will be created in the namespace directory.

        results_dir_symlink_to (Path | None):
            Path to which the results directory should be symlinked.
            If provided, the `results` directory will be a symlink to this path.
            If None, the `results` directory will be created in the namespace directory.

        setup_logging (bool):
            If True, logging will be set up for the unit.
            The logs will be stored in the `logs` directory.

        logging_config (LogConfig | None):
            Optional logging configuration.
            If None and `setup_logging` is True, a default configuration will be created.

        logging_kwargs (Dict[str, Any] | None):
            kwargs to pass to LogConfig if `logging_config` is None.
            Useful for customizing logging setup without creating a LogConfig manually.
    """

    def __init__(
        self,
        namespace: str,  # was `module_name` in earlier versions
        instance_name: str | None = None,
        *,
        working_root: Path | None = None,  # new preferred argument
        data_dir: Path | None = None,  # deprecated alias for `working_root`
        raw_dir_symlink_to: Path | None = None,
        processed_dir_symlink_to: Path | None = None,
        logs_dir_symlink_to: Path | None = None,
        results_dir_symlink_to: Path | None = None,
        setup_logging: bool = True,
        logging_config: Optional["LogConfig"] = None,
        logging_kwargs: Dict[str, Any] | None = None,
    ):
        # If working_root is None, use the current working directory
        working_root = self.handle_working_root_data_dir(
            working_root, data_dir
        )
        if working_root is None:
            working_root = Path.cwd()
        self.working_root = working_root.expanduser().resolve()

        # Set up namespace and instance directories
        self.namespace = namespace
        self.instance_name = instance_name
        # top_level_dir => working_root/namespace/[instance_name/]
        self.top_level_dir = self.working_root / self.namespace
        if self.instance_name is not None:
            self.top_level_dir = self.top_level_dir / self.instance_name
        self.top_level_dir.mkdir(parents=True, exist_ok=True)

        # Set up raw, processed, logs, and results directories
        for dir_name, symlink_to in [
            ("raw", raw_dir_symlink_to),
            ("processed", processed_dir_symlink_to),
            ("logs", logs_dir_symlink_to),
            ("results", results_dir_symlink_to)
        ]:
            dir_path = self.top_level_dir / dir_name

            if symlink_to is not None:
                symlink_to = symlink_to.expanduser().resolve(strict=True)

                # Ensure symlink_to is a directory (and exists)
                if not symlink_to.is_dir():
                    raise FileNotFoundError(
                        f"{symlink_to} is not a directory."
                    )

                # Remove the old symlink if it exists
                if dir_path.exists():
                    if dir_path.is_symlink():
                        dir_path.unlink()
                    else:
                        raise FileExistsError(
                            f"{dir_path} exists and is not a symlink."
                        )

                # Create the new symlink
                dir_path.symlink_to(
                    symlink_to,
                    target_is_directory=True
                )
            else:
                dir_path.mkdir(parents=True, exist_ok=True)

        # Logging
        self._log_helper: LogHelper | None = None
        if setup_logging:
            if logging_config is None:
                name = (
                    f"{self.namespace}/{self.instance_name}" if self.instance_name is not None
                    else self.namespace
                )
                log_path = self.logs / f"{self.namespace}.log"
                logging_config = LogConfig(
                    name=name,
                    log_path=log_path,
                    **(logging_kwargs or {}),
                )
            self._log_helper = LogHelper(logging_config)

        self.logger.info(
            f"IOHelper initialized with top-level directory: {self.top_level_dir}"
        )

    def create_symlink(
        self,
        dir_name: Literal["raw", "processed"],
        target: Path,
        link_name: Optional[str] = None,
    ):
        """
        Create a symlink in the specified directory (raw or processed).

        Args:
            dir_name (Literal["raw", "processed"]):
                Directory in which to create the symlink.
                Must be either "raw" or "processed".

            target (Path):
                Path to which the symlink should point.

            link_name (Optional[str]):
                Name of the symlink to be created in the specified directory.
                If None, the name of the target file will be used.

        Returns:
            link_path (Path):
                Path of the created symlink.
        """
        if dir_name not in ("raw", "processed"):
            raise ValueError("dir_name must be either 'raw' or 'processed'.")

        target = target.expanduser().resolve(strict=True)
        if not target.exists():
            raise FileNotFoundError(f"Target {target} does not exist.")

        if link_name is None:
            link_name = target.name
        link_path = self.top_level_dir / dir_name / link_name

        if link_path.exists():
            if link_path.is_symlink():
                link_path.unlink()
            else:
                raise FileExistsError(
                    f"{link_path} exists and is not a symlink."
                )

        link_path.symlink_to(target, target_is_directory=target.is_dir())

        self.logger.info(f"Created symlink {link_path} -> {target}")

        return link_path

    @staticmethod
    def handle_working_root_data_dir(
        working_root: Path | None,
        data_dir: Path | None,
    ):
        """
        Handle the deprecated `data_dir` argument
        """

        import warnings

        # If both working_root and data_dir are provided, ensure they are the same
        if data_dir is not None and working_root is not None:
            if data_dir != working_root:
                raise ValueError(
                    "Cannot specify both 'data_dir' and 'working_root' with different values."
                )

        # If data_dir is provided, issue a deprecation warning
        if data_dir is not None:
            warnings.warn(
                "'data_dir' is deprecated. Use 'working_root' instead.",
                category=DeprecationWarning,
                stacklevel=2,
            )

        # If working_root is None, use data_dir
        if working_root is None:
            working_root = data_dir

        return working_root

    @property
    def raw(self):
        return self.top_level_dir / "raw"

    @property
    def processed(self):
        return self.top_level_dir / "processed"

    @property
    def logs(self):
        return self.top_level_dir / "logs"

    @property
    def results(self):
        return self.top_level_dir / "results"

    @property
    def has_logger(self):
        """
        True if the instance has a logger set up.
        """
        return self._log_helper is not None

    @property
    def log_helper(self) -> "LogHelper":
        """
        Get the LogHelper for the instance.
        Raises an error if no LogHelper is set up.
        """
        if not self.has_logger:
            raise RuntimeError("No LogHelper is set up for this instance.")
        return self._log_helper

    @property
    def logger(self) -> logging.LoggerAdapter:
        """
        Get the logger for the instance.
        If no logger is set up, returns a no-op logger.

        This allows code to call, for example, `.logger.info(...)`
        without checking if the logger exists.
        """
        return self._log_helper.logger if self.has_logger else _NOOP_LOGGER

    def get_worker_logging_config(self):
        """
        Re-expose `LogHelper.get_worker_config()` for convenience.
        """
        if not self.has_logger:
            raise RuntimeError(
                "Attempt to get worker logging config when no logger is set up."
            )
        return self._log_helper.get_worker_config()


class _CtxLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, ctx_str: str):
        super().__init__(logger, {"ctx": ctx_str})

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("ctx", self.extra.get("ctx", ""))
        return msg, kwargs


class _EnsureCtx(logging.Filter):
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
    if not any(isinstance(f, _EnsureCtx) for f in h.filters):
        h.addFilter(_EnsureCtx())
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
