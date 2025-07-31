from pathlib import Path
import pandas as pd


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

        **kwargs (dict):
            Additional arguments for logging setup.
            See the `setup_logging` method for more details.
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
        **kwargs,
    ):
        # If working_root is None, use the current working directory
        working_root = self.handle_working_root_data_dir(
            working_root, data_dir
        )
        if working_root is None:
            working_root = Path.cwd()
        self.working_root = working_root.expanduser().resolve(strict=True)

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

        if setup_logging:
            self.logger = self.setup_logging(**kwargs)

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
    
    def setup_logging(self, **kwargs):
        from datetime import datetime
        import logging

        logger_name = (
            f"{self.namespace}/{self.instance_name}" if self.instance_name is not None
            else self.namespace
        )
        logger = logging.getLogger(logger_name)

        logger.propagate = False
        # Set top-level logging level to DEBUG to capture all logs
        logger.setLevel(logging.DEBUG)
        if not logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s {%(name)s} "
                "(%(module)s:%(lineno)d) [%(funcName)s] %(message)s",
                datefmt='%Y-%m-%d %H:%M:%S'
            )

            # Console handler for output to the terminal.
            console_handler = logging.StreamHandler()
            console_handler.setLevel(kwargs.get(
                "log_level_console",
                logging.WARNING
            ))
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

            # File handler
            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            file_handler = logging.FileHandler(self.logs / filename)
            file_handler.setLevel(kwargs.get(
                "log_level_file",
                logging.INFO
            ))
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        return logger

    def write_processed_pickle(self, filename: str, data: object):
        import pickle

        self.processed.joinpath(filename).write_bytes(
            pickle.dumps(data)
        )

    def read_processed_pickle(self, filename: str) -> object:
        import pickle

        return pickle.loads(
            self.processed.joinpath(filename).read_bytes()
        )

    def write_processed_df_csv(self, filename: str, df: pd.DataFrame, **kwargs):
        df.to_csv(self.processed / filename, **kwargs)

    def read_processed_df_csv(self, filename: str, **kwargs) -> pd.DataFrame:
        return pd.read_csv(self.processed / filename, **kwargs)
