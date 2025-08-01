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
        module_name (str):
            Name of the module.
            This is used to create a directory structure for data storage.

        instance_name (str | None):
            Name of the instance.
            If provided, it will be used to create a subdirectory under the module directory.
            If None, the module directory will be used directly.

        data_dir (Path | None):
            Base directory for data files. If None, the current working directory will be used.
            The directory structure will be created as `data_dir/module_name/[instance_name/]`

        raw_dir_symlink_to (Path | None):
            Path to which the raw directory should be symlinked.
            If provided, the `raw` directory will be a symlink to this path.
            If None, the `raw` directory will be created in the module directory.

        processed_dir_symlink_to (Path | None):
            Path to which the processed directory should be symlinked.
            If provided, the `processed` directory will be a symlink to this path.
            If None, the `processed` directory will be created in the module directory.

        logs_dir_symlink_to (Path | None):
            Path to which the logs directory should be symlinked.
            If provided, the `logs` directory will be a symlink to this path.
            If None, the `logs` directory will be created in the module directory.

        results_dir_symlink_to (Path | None):
            Path to which the results directory should be symlinked.
            If provided, the `results` directory will be a symlink to this path.
            If None, the `results` directory will be created in the module directory.

        setup_logging (bool):
            If True, logging will be set up for the module.
            The logs will be stored in the `logs` directory.

        **kwargs (dict):
            Additional arguments for logging setup.
            See the `setup_logging` method for more details.
    """

    def __init__(
        self,
        module_name: str,
        instance_name: str | None = None,
        data_dir: Path | None = None,
        raw_dir_symlink_to: Path | None = None,
        processed_dir_symlink_to: Path | None = None,
        logs_dir_symlink_to: Path | None = None,
        results_dir_symlink_to: Path | None = None,
        setup_logging: bool = True,
        **kwargs,
    ):
        # Directory structure: data_dir/module_name/[instance_name/]
        self.module_name = module_name
        self.instance_name = instance_name

        # Set up module directory
        if data_dir is None:
            data_dir = Path.cwd()
        self.module_dir = data_dir.expanduser().resolve(strict=True) / self.module_name
        if self.instance_name is not None:
            self.module_dir = self.module_dir / self.instance_name
        self.module_dir.mkdir(parents=True, exist_ok=True)

        # Set up raw, processed, logs, and results directories
        for dir_name, symlink_to in [
            ("raw", raw_dir_symlink_to),
            ("processed", processed_dir_symlink_to),
            ("logs", logs_dir_symlink_to),
            ("results", results_dir_symlink_to)
        ]:
            dir_path = self.module_dir / dir_name

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

    @property
    def raw(self):
        return self.module_dir / "raw"

    @property
    def processed(self):
        return self.module_dir / "processed"

    @property
    def logs(self):
        return self.module_dir / "logs"

    @property
    def results(self):
        return self.module_dir / "results"

    def setup_logging(self, **kwargs):
        from datetime import datetime
        import logging

        logger_name = (
            f"{self.module_name}/{self.instance_name}" if self.instance_name is not None
            else self.module_name
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
