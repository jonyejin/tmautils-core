import sqlite3
import re
import logging
from dataclasses import dataclass
from dataclasses_json import DataClassJsonMixin

from imresearchutils.common import *
from .types import *

neterror_pattern = re.compile(
    r"Received neterror (?P<error_type>\w+) while executing command: "
    r"BrowseCommand\(http://(?P<url>.*),5,3\)"
)


@dataclass
class CrawlProgress(DataClassJsonMixin):
    chunks_done: int = 0


class OpenWpmCrawlUtil:
    """
    A utility class for crawling a list of websites using OpenWPM.

    Args:
        openwpm_path (Path):
            Path to the OpenWPM installation directory.

        crawl_id (str):
            Unique identifier for the crawl instance.
            Useful since crawls can span days and be resumed.

        sites (list[str] | None):
            List of sites to crawl.
            If None, the sites will be read from `sites.txt` in the raw directory.
            Passing None is useful for resuming crawls.

        n_browsers (int):
            Number of browsers to use for crawling.
            Default is 10.

        n_click_internal_links (int):
            Number of internal links to click on main page.
            Default is 5.

        browser_sleep_dur (int):
            Sleep duration (in seconds) passed to BrowseCommand.
            Default is 3.

        failure_limit (int | None):
            Maximum number of consecutive failures before stopping the crawl.
            If None, the default OpenWPM limit will be used.

        n_sites_chunk (int):
            Number of sites to crawl in each chunk.
            Default is 100.

        max_retry_per_chunk (int):
            Maximum number of retries for each chunk.
            Default is 3.

        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    COMPRESSION_MAX_WORKERS = 2

    def __init__(
        self,
        openwpm_path: Path,
        crawl_id: str,
        sites: list[str] | None = None,
        n_browsers: int = 10,
        n_click_internal_links: int = 5,
        browser_sleep_dur: int = 3,
        failure_limit: int | None = None,
        n_sites_chunk: int = 100,
        max_retry_per_chunk: int = 3,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        **kwargs,
    ):
        import sys
        import math

        # Update sys.path to include the OpenWPM path
        self.openwpm_path = openwpm_path.expanduser().resolve()
        if not self.openwpm_path.is_dir():
            raise ValueError(
                f"Provided path {self.openwpm_path} is not a valid directory."
            )
        if str(self.openwpm_path) not in sys.path:
            sys.path.insert(0, str(self.openwpm_path))

        # Import OpenWPM modules
        openwpm_modules = [
            ("openwpm.command_sequence", "CommandSequence"),
            ("openwpm.commands.browser_commands", "BrowseCommand"),
            ("openwpm.config", "BrowserParams"),
            ("openwpm.config", "ManagerParams"),
            ("openwpm.storage.sql_provider", "SQLiteStorageProvider"),
            ("openwpm.task_manager", "TaskManager"),
        ]
        for module, attr in openwpm_modules:
            setattr(self, attr, import_module_attr(module, attr))

        self.crawl_id = crawl_id
        self.n_browsers = n_browsers
        self.n_click_internal_links = n_click_internal_links
        self.browser_sleep_dur = browser_sleep_dur
        self.failure_limit = failure_limit
        self.n_sites_chunk = n_sites_chunk
        self.max_retry_per_chunk = max_retry_per_chunk

        # Set up data directory
        working_root = IOHelper.handle_working_root_data_dir(
            working_root, data_dir
        )
        self.io_helper = IOHelper(
            self.__class__.__name__,
            instance_name=self.crawl_id,
            working_root=working_root,
            **kwargs,
        )

        # Initialize the sites list
        sites_path = self.io_helper.raw / "sites.txt"
        if sites_path.exists():
            self.sites = sites_path.read_text().splitlines()
        elif sites is not None:
            self.sites = sites
            sites_path.write_text("\n".join(self.sites) + "\n")
        else:
            raise ValueError(
                "No sites provided and 'sites.txt' not found in raw directory."
            )
        self.n_chunks = math.ceil(len(self.sites) / n_sites_chunk)

        # Load progress if resuming
        self.progress_path = self.io_helper.raw / "progress"
        if self.progress_path.exists():
            self.progress = CrawlProgress.from_json(
                self.progress_path.read_text()
            )
            self.io_helper.logger.info(
                f"Resuming crawl from chunk {self.progress.chunks_done}"
            )
        else:
            self.progress = CrawlProgress()

        # Remove incomplete crawl database if it exists
        for extension in ('sqlite', 'sqlite-journal'):
            self.io_helper.raw.joinpath(
                f"crawl_chunk_{self.progress.chunks_done}.{extension}"
            ).unlink(missing_ok=True)

        self.openwpm_log_path = (
            self.io_helper.raw / "openwpm.log"
        )
        self.log_pos = 0

        self.io_helper.logger.info(
            f"OpenWPM crawl utility initialized with "
            f"{len(self.sites)} sites, "
            f"{self.n_browsers} browsers, "
            f"{self.n_click_internal_links} internal link clicks per site, "
            f"{self.browser_sleep_dur} seconds sleep duration, "
            f"{self.failure_limit} failure limit, "
            f"{self.n_sites_chunk} sites per chunk, and "
            f"{self.max_retry_per_chunk} retries per chunk."
        )

    def crawl_chunk(
        self,
        chunknum: int,
    ):
        """
        Crawl a chunk of sites.

        Args:
            chunknum (int):
                Chunk number to crawl.
        """

        site_ranks = range(
            chunknum*self.n_sites_chunk,
            min((chunknum+1)*self.n_sites_chunk, len(self.sites))
        )
        sites_to_crawl = list(zip(
            site_ranks,
            [self.sites[s] for s in site_ranks]
        ))

        db_path = self.io_helper.raw / f"crawl_chunk_{chunknum}.sqlite"

        for trynum in range(self.max_retry_per_chunk):
            if trynum > 0:
                self.io_helper.logger.warning(
                    f"Crawling failed for {sites_to_crawl}, retrying"
                )

            num_browsers = min(self.n_browsers, len(sites_to_crawl))
            manager_params = self.ManagerParams(
                num_browsers=num_browsers,
                data_directory=self.io_helper.raw,
                log_path=self.openwpm_log_path,
                process_watchdog=True,
                memory_watchdog=True,
                _failure_limit=self.failure_limit,
            )
            browser_params = [
                self.BrowserParams(
                    display_mode="xvfb",
                    cookie_instrument=True,
                    js_instrument=True,
                    http_instrument=True,
                    navigation_instrument=True,
                    dns_instrument=True,
                    bot_mitigation=True,
                )
                for _ in range(num_browsers)
            ]

            # Do the crawling
            with self.TaskManager(
                manager_params,
                browser_params,
                self.SQLiteStorageProvider(db_path),
                None,
                logger_kwargs={
                    'log_level_console': logging.CRITICAL,
                    'log_level_file': logging.INFO,
                }
            ) as task_manager:
                for (site_rank, site) in sites_to_crawl:
                    self.io_helper.logger.info(
                        f"Starting crawl for site {site} "
                        f"({site_rank+1} out of {len(self.sites)})"
                    )
                    command_sequence = self.CommandSequence(
                        site,
                        site_rank=site_rank,
                        reset=True,
                    )
                    command_sequence.append_command(
                        self.BrowseCommand(
                            url=site,
                            num_links=self.n_click_internal_links,
                            sleep=self.browser_sleep_dur,
                        ),
                        timeout=30,
                    )
                    task_manager.execute_command_sequence(command_sequence)

            # Find sites that failed to crawl
            missing_sites: set[str] = set()
            with sqlite3.connect(db_path) as db_conn:
                missing_sites.update(map(
                    lambda x: OpenWpmSiteCrawlResult(*x).fqdn.name,
                    db_conn.execute(
                        """
                        SELECT sv.site_rank, sv.site_url, sv.visit_id FROM site_visits sv
                        WHERE sv.visit_id NOT IN (SELECT DISTINCT visit_id FROM dns_responses)
                        ORDER BY sv.site_rank
                        """
                    ).fetchall()
                ))
            self.io_helper.logger.info(
                f"Sites missing from database after crawl: {missing_sites}"
            )

            # Do not retry sites that failed due to network problems
            neterrors = set()
            with open(self.openwpm_log_path) as log:
                log.seek(self.log_pos)
                for line in log:
                    if (m := neterror_pattern.search(line)) is not None:
                        neterrors.add(m.groupdict()['url'])
                self.log_pos = log.tell()
            self.io_helper.logger.info(
                f"Sites with network errors: {neterrors}. "
                f"Not retrying these sites."
            )
            missing_sites.difference_update(neterrors)

            # Update set of sites to retry
            sites_to_crawl = list(filter(
                lambda s: s[1][7:] in missing_sites,
                sites_to_crawl
            ))
            if not sites_to_crawl:
                break

        if sites_to_crawl:
            self.io_helper.logger.warning(
                f"Failed to crawl sites {sites_to_crawl} after "
                f"{self.max_retry_per_chunk} retries."
            )
        else:
            self.io_helper.logger.info(
                f"Successfully crawled all sites in chunk {chunknum}."
            )

        # Compress the chunk database file
        gzip_file(db_path, force=True, logger=self.io_helper.logger)

    def compress_crawled_chunks(
        self,
        delete_original: bool = True,
    ):
        """
        Compress all crawled chunk databases in the raw directory in parallel.

        Args:
            delete_original (bool):
                If True, the original SQLite files will be deleted after compression.
                Default is True.
        """

        from concurrent.futures import ThreadPoolExecutor, as_completed

        paths = list(self.io_helper.raw.glob("crawl_chunk_*.sqlite"))
        if not paths:
            return

        with ThreadPoolExecutor(max_workers=self.COMPRESSION_MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    gzip_file, p, delete_original=delete_original, logger=self.io_helper.logger
                ): p for p in paths
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    self.io_helper.logger.error(
                        f"Failed to compress {futures[future]}: {repr(future.exception())}"
                    )

    def crawl(self):
        """
        Crawl the list of sites in chunks.
        """

        # Compress any previously completed crawl chunks
        self.compress_crawled_chunks()

        if self.is_crawl_done():
            return

        for chunknum in range(self.progress.chunks_done, self.n_chunks):
            self.io_helper.logger.info(
                f"Starting crawl for chunk {chunknum}"
            )
            try:
                self.crawl_chunk(chunknum)
            except Exception as e:
                self.io_helper.logger.error(
                    f"Error crawling chunk {chunknum}: {repr(e)}"
                )
                raise
            self.progress.chunks_done += 1
            self.progress_path.write_text(self.progress.to_json())

    def is_crawl_done(self) -> bool:
        return self.progress.chunks_done >= self.n_chunks
