import sys
import sqlite3
import logging
import re

import tranco
from dataclasses import dataclass
from dataclasses_json import DataClassJsonMixin

from imresearchutils.common import *
from .types import TrancoSiteCrawlResult

neterror_pattern = re.compile(
    r"Received neterror (?P<error_type>\w+) while executing command: "
    r"BrowseCommand\(http://(?P<url>.*),5,3\)"
)


@dataclass
class CrawlProgress(DataClassJsonMixin):
    chunks_done: int = 0


class TrancoCrawlUtil:
    def __init__(
        self,
        openwpm_path: Path,
        tranco_list_id: str | None = None,
        data_dir: Path | None = None,
        repeat: int = 1,
        n_top_sites: int = 100000,
        n_browsers: int = 10,
        n_click_internal_links: int = 5,
        browser_sleep_dur: int = 3,
        n_sites_chunk: int = 100,
        max_retry_per_chunk: int = 3,
    ):
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

        self.n_top_sites = n_top_sites
        self.n_browsers = n_browsers
        self.n_click_internal_links = n_click_internal_links
        self.browser_sleep_dur = browser_sleep_dur
        self.n_sites_chunk = n_sites_chunk
        self.n_chunks = n_top_sites // n_sites_chunk
        self.max_retry_per_chunk = max_retry_per_chunk

        # Set up Tranco list
        if data_dir is not None:
            cache_dir = data_dir / ".tmp" / "tranco_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            cache_dir = None
        self.tranco_list = tranco.Tranco(
            cache_dir=cache_dir,
        ).list(list_id=tranco_list_id)
        self.top_sites: list[str] = [
            f"http://{x}" for x in self.tranco_list.top(self.n_top_sites)
        ]

        # Set up data directory
        instance_name = (
            self.tranco_list.list_id if repeat == 1 else f"{self.tranco_list.list_id}_{repeat}"
        )
        self.io_helper = IOHelper(
            module_name=self.__class__.__name__,
            instance_name=instance_name,
            data_dir=data_dir
        )
        self.io_helper.logger.info(
            f"Using Tranco list with id {self.tranco_list.list_id} "
            f"and date {self.tranco_list.date}"
        )

        # Load progress if resuming
        progress = self.io_helper.raw / "progress"
        if progress.exists():
            self.progress = CrawlProgress.from_json(progress.read_text())
            self.io_helper.logger.info(
                f"Resuming crawl: Tranco list id {self.tranco_list.list_id}"
            )
        else:
            self.progress = CrawlProgress()

        # Remove incomplete crawl database if it exists
        for extension in ('sqlite', 'sqlite-journal'):
            self.io_helper.raw.joinpath(
                f"serverside_chunk_{self.progress.chunks_done}.{extension}"
            ).unlink(missing_ok=True)

        self.openwpm_log_path = (
            self.io_helper.raw / f"{self.tranco_list.list_id}.log"
        )
        self.log_pos = 0
        self.io_helper.logger.info(
            "Initialization complete."
        )

    def crawl_chunk(
        self,
        manager_params,
        browser_params,
        chunknum: int,
    ):
        site_ranks = range(
            chunknum*self.n_sites_chunk,
            (chunknum+1)*self.n_sites_chunk
        )
        sites_to_crawl = list(zip(
            site_ranks,
            [self.top_sites[s] for s in site_ranks]
        ))

        db_path = self.io_helper.raw / f"serverside_chunk_{chunknum}.sqlite"
        db_cursor = sqlite3.connect(db_path.absolute()).cursor()

        for trynum in range(self.max_retry_per_chunk):
            if trynum > 0:
                self.io_helper.logger.warning(
                    f"Crawling failed for {sites_to_crawl}, retrying"
                )

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
                        f"({site_rank} out of {self.n_top_sites})"
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
            missing_sites.update(map(
                lambda x: TrancoSiteCrawlResult(*x).fqdn.name,
                db_cursor.execute(
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

    def crawl(self):
        manager_params = self.ManagerParams(
            num_browsers=self.n_browsers,
            data_directory=self.io_helper.raw,
            log_path=self.openwpm_log_path,
            process_watchdog=True,
            memory_watchdog=True,
        )
        browser_params = [
            self.BrowserParams(
                display_mode="xvfb",
                http_instrument=True,
                dns_instrument=True,
                bot_mitigation=True,
                cookie_instrument=False,
            )
            for _ in range(self.n_browsers)
        ]

        for chunknum in range(self.progress.chunks_done, self.n_chunks):
            self.io_helper.logger.info(
                f"Starting crawl for chunk {chunknum}"
            )
            try:
                self.crawl_chunk(manager_params, browser_params, chunknum)
            except Exception as e:
                self.io_helper.logger.error(
                    f"Error crawling chunk {chunknum}, quitting: {repr(e)}"
                )
                break
            self.progress.chunks_done += 1
            self.io_helper.raw.joinpath(
                'progress').write_text(self.progress.to_json())
