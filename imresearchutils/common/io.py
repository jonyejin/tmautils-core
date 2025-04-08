from pathlib import Path
from datetime import datetime
import logging


class IOHelper:
    def __init__(self,
                 module_name: str,
                 data_dir: Path | None = None,
                 setup_logging: bool = True,
                 **kwargs):
        self.module_name = module_name

        if data_dir is None:
            data_dir = Path.cwd()
        self.module_dir = data_dir / module_name
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
