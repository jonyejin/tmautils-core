# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Any, Dict, Optional, TYPE_CHECKING
from pathlib import Path
from logging import Logger
from dataclasses import dataclass, field
from enum import StrEnum

from .log import LogConfig, LogHelper, get_logger_from_helper


class DirCreationMode(StrEnum):
    """
    Mode for directory creation timing.

    Choose EAGER to create directories at initialization (legacy behavior),
    or LAZY to create them on first access (new default).
    """

    EAGER = "eager"  # Create at init (legacy behavior)
    LAZY = "lazy"  # Create on first access (new default)


@dataclass(slots=True)
class DirConfig:
    """
    Configuration for a single directory.

    Args:
        enabled: Whether this directory is enabled (can be accessed).
        symlink_to: Path to symlink this directory to.
            If set, the directory will be a symlink to this path.
        creation_mode: When to create the directory (EAGER or LAZY).
    """

    enabled: bool = True
    symlink_to: Optional[Path] = None
    creation_mode: DirCreationMode = DirCreationMode.LAZY


@dataclass(slots=True)
class IOConfig:
    """
    Configuration for IOHelper.

    Args:
        top_level_symlink_to: Path to symlink the entire top-level directory to.
            If set, namespace/[instance_name]/ will be a symlink.
        raw: Configuration for the `raw/` directory.
        processed: Configuration for the `processed/` directory.
        logs: Configuration for the `logs/` directory.
        custom_dirs: Additional directories (e.g., `{"results": DirConfig()}`).
        setup_logging: Whether to set up logging.
        logging_config: Optional LogConfig for custom logging setup.
        logging_kwargs: kwargs passed to LogConfig if logging_config is None.
    """

    top_level_symlink_to: Path | None = None

    raw: DirConfig = field(default_factory=DirConfig)
    processed: DirConfig = field(default_factory=DirConfig)
    logs: DirConfig = field(default_factory=DirConfig)
    custom_dirs: dict[str, DirConfig] = field(default_factory=dict)

    setup_logging: bool = True
    logging_config: LogConfig | None = None
    logging_kwargs: dict[str, Any] = field(default_factory=dict)


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
    logger: Logger | None = None,
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
    logger: Logger | None = None,
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

    There are two ways to use IOHelper:

    1. **New API** (recommended): Pass an `IOConfig` object.
       More flexible and configurable.
       Only creates configured directories, lazily on first access.

    2. **Legacy API**: Pass individual parameters.
       Creates `raw`, `processed`, `logs` and `results` directories eagerly at init.

    Args:
        namespace (str):
            Name of the namespace for this unit.
            It becomes a directory under `working_root` (i.e., `working_root/namespace/`).

        instance_name (str | None):
            Optional instance name.
            If provided, it will be used to create a subdirectory under the namespace directory.

        config (IOConfig | None):
            Configuration object for new API.
            If provided, uses IOConfig settings and ignores legacy symlink parameters.
            Provides lazy creation, custom directories, and top-level symlinks.

        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            [Legacy] Deprecated alias for `working_root`.

        raw_dir_symlink_to (Path | None):
            [Legacy] Path to which the raw directory should be symlinked.

        processed_dir_symlink_to (Path | None):
            [Legacy] Path to which the processed directory should be symlinked.

        logs_dir_symlink_to (Path | None):
            [Legacy] Path to which the logs directory should be symlinked.

        results_dir_symlink_to (Path | None):
            [Legacy] Path to which the results directory should be symlinked.

        setup_logging (bool):
            [Legacy] If True, logging will be set up for the unit.

        logging_config (LogConfig | None):
            [Legacy] Optional logging configuration.

        logging_kwargs (Dict[str, Any] | None):
            [Legacy] kwargs to pass to LogConfig if `logging_config` is None.
    """

    _STANDARD_DIRS = frozenset({"raw", "processed", "logs"})

    # For IDE typing support (these are accessed via __getattr__)
    if TYPE_CHECKING:
        raw: Path
        processed: Path
        logs: Path
        results: Path

    def __init__(
        self,
        namespace: str,
        instance_name: str | None = None,
        *,
        config: IOConfig | None = None,
        working_root: Path | None = None,
        data_dir: Path | None = None,  # deprecated alias for `working_root`
        # Legacy parameters: Use config instead
        raw_dir_symlink_to: Path | None = None,
        processed_dir_symlink_to: Path | None = None,
        logs_dir_symlink_to: Path | None = None,
        results_dir_symlink_to: Path | None = None,
        setup_logging: bool = True,
        logging_config: LogConfig | None = None,
        logging_kwargs: Dict[str, Any] | None = None,
    ):
        # Handle working_root vs deprecated data_dir
        working_root = self.handle_working_root_data_dir(
            working_root, data_dir
        )
        if working_root is None:
            working_root = Path.cwd()
        self.working_root = working_root.expanduser().resolve()

        # Set up namespace and instance directories
        self.namespace = namespace
        self.instance_name = instance_name

        # Set up top-level directory (possibly as symlink)
        self.top_level_dir = self.working_root / self.namespace
        if self.instance_name is not None:
            self.top_level_dir = self.top_level_dir / self.instance_name
        self._setup_top_level_dir(
            top_level_symlink_to=(
                config.top_level_symlink_to if config is not None else None
            )
        )

        # Set up sub-directories
        self._dir_configs = self._build_dir_configs(
            config=config,
            raw_dir_symlink_to=raw_dir_symlink_to,
            processed_dir_symlink_to=processed_dir_symlink_to,
            logs_dir_symlink_to=logs_dir_symlink_to,
            results_dir_symlink_to=results_dir_symlink_to,
        )
        self._dirs_created: set[str] = set()
        self._create_eager_dirs()

        # Set up logging if requested
        self._log_helper: LogHelper | None = None
        _setup_logging = (
            config.setup_logging if config is not None else setup_logging
        )
        if _setup_logging:
            self._setup_logging(
                logging_config=(
                    config.logging_config if config is not None else logging_config
                ),
                logging_kwargs=(
                    config.logging_kwargs if config is not None else (
                        logging_kwargs or {}
                    )
                ),
            )

        self.logger.info(
            f"IOHelper initialized with top-level directory: {self.top_level_dir}"
        )

    @classmethod
    def init_with_dirs(
        cls,
        namespace: str,
        dirs: set[str],
        *,
        working_root: Path | None = None,
        **kwargs,
    ) -> "IOHelper":
        """
        Create IOHelper with a fixed set of directories.

        This classmethod is designed for use by utility classes that define a fixed
        set of directories they need.
        Users of the utility can customize symlinks and logging,
        but cannot add or remove directories.

        Args:
            namespace: Name of the namespace directory.
            dirs: Set of enabled directory names.
            working_root: Base directory.
            **kwargs: User customizations:
                - instance_name: Optional instance name
                - top_level_symlink_to: Path to symlink the namespace dir to
                - {dir}_dir_symlink_to: Path to symlink any dir in `dirs` to
                - setup_logging: Whether to set up logging (default True)
                - logging_config: Optional LogConfig
                - logging_kwargs: kwargs for LogConfig

        Returns:
            IOHelper instance.

        Raises:
            ValueError: If symlink kwargs are provided for dirs not in `dirs`.

        Example:
            >>> # Utility defines its structure
            >>> class MyUtil:
            ...     def __init__(self, working_root=None, **kwargs):
            ...         self._io_helper = IOHelper.init_with_dirs(
            ...             self.__class__.__name__,
            ...             dirs={"raw", "logs"},
            ...             working_root=working_root,
            ...             **kwargs,
            ...         )
            >>> # User can customize symlinks/logging
            >>> util = MyUtil(
            ...     working_root=Path("/data"),
            ...     raw_dir_symlink_to=Path("/tmp/raw"),
            ...     setup_logging=False,
            ... )
        """
        config, instance_name = cls._build_config_from_dirs(dirs, kwargs)
        return cls(
            namespace,
            instance_name=instance_name,
            config=config,
            working_root=working_root,
        )

    @staticmethod
    def _build_config_from_dirs(
        dirs: set[str],
        kwargs: dict,
    ) -> tuple[IOConfig, str | None]:
        # Separate standard and custom dirs
        custom_dir_names = dirs - IOHelper._STANDARD_DIRS

        # Extract symlinks for dirs in the set
        symlinks: dict[str, Path] = {}
        for dir_name in dirs:
            kwarg_name = f"{dir_name}_dir_symlink_to"
            if kwarg_name in kwargs:
                symlinks[dir_name] = kwargs.pop(kwarg_name)

        # Reject symlink kwargs for dirs NOT in set
        for key in list(kwargs.keys()):
            if key.endswith("_dir_symlink_to"):
                dir_name = key.removesuffix("_dir_symlink_to")
                raise ValueError(
                    f"Cannot symlink '{dir_name}' - not in dirs. Valid: {dirs}"
                )

        # Extract top-level symlink
        top_level_symlink = kwargs.pop("top_level_symlink_to", None)

        # Extract logging config
        setup_logging = kwargs.pop("setup_logging", True)
        logging_config = kwargs.pop("logging_config", None)
        logging_kwargs = kwargs.pop("logging_kwargs", {})

        # Extract instance_name
        instance_name = kwargs.pop("instance_name", None)

        # Warn about unknown kwargs
        if kwargs:
            import warnings
            warnings.warn(f"Unknown kwargs: {list(kwargs.keys())}")

        # Build IOConfig
        config = IOConfig(
            top_level_symlink_to=top_level_symlink,
            raw=DirConfig(
                enabled="raw" in dirs,
                symlink_to=symlinks.get("raw")),
            processed=DirConfig(
                enabled="processed" in dirs,
                symlink_to=symlinks.get("processed")),
            logs=DirConfig(
                enabled="logs" in dirs,
                symlink_to=symlinks.get("logs")),
            custom_dirs={
                name: DirConfig(symlink_to=symlinks.get(name))
                for name in custom_dir_names
            },
            setup_logging=setup_logging,
            logging_config=logging_config,
            logging_kwargs=logging_kwargs,
        )
        return config, instance_name

    def _build_dir_configs(
        self,
        *,
        config: IOConfig | None,
        raw_dir_symlink_to: Path | None,
        processed_dir_symlink_to: Path | None,
        logs_dir_symlink_to: Path | None,
        results_dir_symlink_to: Path | None,
    ) -> dict[str, DirConfig]:
        # If config object is provided, use it
        if config is not None:
            dir_configs: dict[str, DirConfig] = {
                "raw": config.raw,
                "processed": config.processed,
                "logs": config.logs,
            }
            for name, cfg in config.custom_dirs.items():
                if name in self._STANDARD_DIRS:
                    raise ValueError(
                        f"Cannot use IOConfig directory name '{name}' in custom_dirs. "
                        f"Configure it directly via config.{name} instead."
                    )
                dir_configs[name] = cfg
            return dir_configs

        # Legacy API: eagerly create (old) standard directories
        return {
            "raw": DirConfig(
                symlink_to=raw_dir_symlink_to,
                creation_mode=DirCreationMode.EAGER,
            ),
            "processed": DirConfig(
                symlink_to=processed_dir_symlink_to,
                creation_mode=DirCreationMode.EAGER,
            ),
            "logs": DirConfig(
                symlink_to=logs_dir_symlink_to,
                creation_mode=DirCreationMode.EAGER,
            ),
            "results": DirConfig(
                symlink_to=results_dir_symlink_to,
                creation_mode=DirCreationMode.EAGER,
            ),
        }

    def _setup_top_level_dir(self, top_level_symlink_to: Path | None = None):
        if top_level_symlink_to is not None:
            self.top_level_dir.parent.mkdir(parents=True, exist_ok=True)
            self._create_dir_symlink(self.top_level_dir, top_level_symlink_to)
        else:
            self.top_level_dir.mkdir(parents=True, exist_ok=True)

    def _create_eager_dirs(self):
        for name, cfg in self._dir_configs.items():
            if cfg.enabled and cfg.creation_mode == DirCreationMode.EAGER:
                self._ensure_dir(name)

    def _get_dir_path(self, name: str) -> Path:
        return self.top_level_dir / name

    def _ensure_dir(self, name: str) -> Path:
        if name in self._dirs_created:
            return self._get_dir_path(name)

        cfg = self._dir_configs.get(name)
        if cfg is None:
            raise AttributeError(
                f"'{type(self).__name__}' has no directory '{name}'"
            )
        if not cfg.enabled:
            raise RuntimeError(
                f"Directory '{name}' is disabled. "
                f"Enable it via DirConfig(enabled=True)."
            )

        path = self._get_dir_path(name)

        if cfg.symlink_to is not None:
            self._create_dir_symlink(path, cfg.symlink_to)
        else:
            path.mkdir(parents=True, exist_ok=True)

        self._dirs_created.add(name)
        return path

    def _create_dir_symlink(self, dir_path: Path, symlink_to: Path) -> None:
        target = symlink_to.expanduser()

        # If the target is a relative path, make it absolute
        if not target.is_absolute():
            target = target.resolve(strict=True)

        # Target must exist
        if not target.exists():
            raise FileNotFoundError(f"{target} does not exist.")

        # Resolved target must be a directory
        resolved = target.resolve(strict=True)
        if not resolved.is_dir():
            raise FileNotFoundError(
                f"{target} does not resolve to a directory."
            )

        # Remove the old symlink if it exists
        if dir_path.exists() or dir_path.is_symlink():
            if dir_path.is_symlink():
                # Just change the symlink
                dir_path.unlink()
            else:
                # No clean way to change a non-symlink dir to a symlink
                raise FileExistsError(
                    f"{dir_path} exists and is not a symlink."
                )

        # Create the new symlink
        dir_path.symlink_to(target, target_is_directory=True)

    def __getattr__(self, name: str) -> Path:
        """
        Access any configured directory (standard or custom) as an attribute.
        """

        # Note: need to short-circuit _dir_configs to avoid infinite recursion
        # (if this method is called before _dir_configs is set up)
        if name != "_dir_configs":
            if name in self._dir_configs:
                return self._ensure_dir(name)

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _setup_logging(
        self,
        logging_config: LogConfig | None,
        logging_kwargs: dict[str, Any],
    ) -> None:
        # Check if logs directory is disabled
        logs_cfg = self._dir_configs.get("logs")
        logs_disabled = logs_cfg is None or not logs_cfg.enabled

        # Determine if file logging is requested
        if logging_config is not None:
            file_logging_enabled = logging_config.file_level is not None
        else:
            # Default LogConfig has file_level=logging.INFO (enabled)
            # Only file_level=None explicitly passed in kwargs disables it
            file_logging_enabled = logging_kwargs.get(
                "file_level", "default"
            ) is not None

        if logs_disabled and file_logging_enabled:
            raise ValueError(
                "Cannot enable file logging when logs directory is disabled. "
                "Either enable the logs directory (logs=DirConfig(enabled=True)) "
                "or disable file logging (logging_kwargs={'file_level': None})."
            )

        if logging_config is None:
            name = (
                f"{self.namespace}/{self.instance_name}"
                if self.instance_name is not None
                else self.namespace
            )
            # Only get logs path if file logging is enabled
            log_path = (
                self.logs / f"{self.namespace}.log"
                if file_logging_enabled
                else None
            )
            logging_config = LogConfig(
                name=name,
                log_path=log_path,
                **logging_kwargs,
            )

        self._log_helper = LogHelper(logging_config)

    def create_symlink(
        self,
        directory: Path,
        target: Path,
        link_name: Optional[str] = None,
    ):
        """
        Create a symlink in the specified directory.

        Args:
            directory (Path):
                Directory in which to create the symlink (e.g., io.raw, io.processed).

            target (Path):
                Path to which the symlink should point.

            link_name (Optional[str]):
                Name of the symlink to be created in the specified directory.
                If None, the name of the target file will be used.

        Returns:
            link_path (Path):
                Path of the created symlink.
        """
        target = target.expanduser()

        # If the target is a relative path, make it absolute
        if not target.is_absolute():
            target = target.resolve(strict=True)

        # Target must exist
        if not target.exists():
            raise FileNotFoundError(f"{target} does not exist.")

        if link_name is None:
            link_name = target.name
        link_path = directory / link_name

        if link_path.exists():
            if link_path.is_symlink():
                link_path.unlink()
            else:
                raise FileExistsError(
                    f"{link_path} exists and is not a symlink."
                )

        # Check if resolved target is directory (for symlink_to flag)
        resolved = target.resolve(strict=True)
        link_path.symlink_to(target, target_is_directory=resolved.is_dir())
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
    def logger(self):
        """
        Get the logger for the instance.
        If no logger is set up, returns a no-op logger which ignores all logging calls.
        """
        return get_logger_from_helper(self._log_helper)

    def get_worker_logging_config(self):
        """
        Re-expose `LogHelper.get_worker_config()` for convenience.
        """
        if not self.has_logger:
            raise RuntimeError(
                "Attempt to get worker logging config when no logger is set up."
            )
        return self._log_helper.get_worker_config()
