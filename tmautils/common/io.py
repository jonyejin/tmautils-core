from typing import Any, Dict, Literal, Optional
from pathlib import Path
from logging import Logger

from .log import LogConfig, LogHelper, get_logger_from_helper

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
