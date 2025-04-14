from pathlib import Path
import importlib
from datetime import datetime
import logging


def import_module_attr(module_path: str, attr_name: str):
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
    def __init__(self,
                 module_name: str,
                 instance_name: str | None = None,
                 data_dir: Path | None = None,
                 setup_logging: bool = True,
                 **kwargs):
        # Directory structure: data_dir/module_name/[instance_name/]
        self.module_name = module_name
        self.instance_name = instance_name

        if data_dir is None:
            data_dir = Path.cwd()
        self.module_dir = data_dir / module_name
        if instance_name is not None:
            self.module_dir = self.module_dir / instance_name
        self.module_dir.mkdir(parents=True, exist_ok=True)

        if setup_logging:
            self.logger = self.setup_logging(**kwargs)

    @property
    def raw(self):
        raw = self.module_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        return raw

    @property
    def processed(self):
        processed = self.module_dir / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        return processed

    @property
    def logs(self):
        logs = self.module_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs

    @property
    def results(self):
        results = self.module_dir / "results"
        results.mkdir(parents=True, exist_ok=True)
        return results

    def setup_logging(self, **kwargs):
        logger = logging.getLogger(self.module_name)
        logger.propagate = False
        # Set top-level logging level to DEBUG to capture all logs
        logger.setLevel(logging.DEBUG)
        if not logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s {%(module)s} [%(funcName)s] %(message)s",
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
