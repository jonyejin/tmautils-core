import sqlite3
import logging
import re
from urllib.parse import urlparse

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
        import sys
        import tranco

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
        self.progress_path = self.io_helper.raw / "progress"
        if self.progress_path.exists():
            self.progress = CrawlProgress.from_json(
                self.progress_path.read_text()
            )
            self.io_helper.logger.info(
                f"Resuming crawl: Tranco list id {self.tranco_list.list_id}"
            )
        else:
            self.progress = CrawlProgress()

        # Remove incomplete crawl database if it exists
        for extension in ('sqlite', 'sqlite-journal'):
            self.io_helper.raw.joinpath(
                f"crawl_chunk_{self.progress.chunks_done}.{extension}"
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
            )
            browser_params = [
                self.BrowserParams(
                    display_mode="xvfb",
                    http_instrument=True,
                    dns_instrument=True,
                    bot_mitigation=True,
                    cookie_instrument=False,
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
            with sqlite3.connect(db_path) as db_conn:
                missing_sites.update(map(
                    lambda x: TrancoSiteCrawlResult(*x).fqdn.name,
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

    def crawl(self):
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


class TrancoProcessUtil:
    def __init__(
        self,
        tranco_list_id: str,
        data_dir: Path | None = None,
        repeat: int = 1,
    ):
        self.tranco_list_id = tranco_list_id

        # Reuse the data directory from the crawl
        instance_name = (
            self.tranco_list_id if repeat == 1 else f"{self.tranco_list_id}_{repeat}"
        )
        self.io_helper = IOHelper(
            module_name=TrancoCrawlUtil.__name__,
            instance_name=instance_name,
            data_dir=data_dir
        )

        # Parse the log file for errors
        self._parse_log_errors()

        # Find number of chunks for convenience
        self.n_chunks = len(list(
            self.io_helper.raw.glob("crawl_chunk_*.sqlite")
        ))

        self.io_helper.logger.info(
            f"Initialized TrancoProcessUtil with list id {self.tranco_list_id} "
            f"and {self.n_chunks} chunks"
        )

    def load_db(
        self,
        chunk_num: int,
        use_cache: bool = True
    ):
        import pickle

        # Check if chunk_num is valid
        chunk_path = self.io_helper.raw / f"crawl_chunk_{chunk_num}.sqlite"
        if not chunk_path.exists():
            raise ValueError(
                f"Chunk {chunk_num} does not exist"
            )

        sites: dict[FQDN, TrancoSiteCrawlResult] = None
        processed_path = (
            self.io_helper.processed / f"{chunk_path.name}.processed"
        )

        # Try to load from cache
        if use_cache:
            try:
                sites = pickle.loads(processed_path.read_bytes())
                self.io_helper.logger.info(
                    f"Loaded {chunk_path.name} from cache"
                )
                return sites
            except:
                self.io_helper.logger.info(
                    f"Cache not found for {chunk_path.name}; processing"
                )

        # If we are here, we need to process the chunk
        cursor = sqlite3.connect(chunk_path).cursor()
        sites = {
            s.fqdn: s for s in map(
                lambda x: TrancoSiteCrawlResult(*x),
                cursor.execute(
                    """
                    SELECT sv.site_rank, sv.site_url, sv.visit_id FROM site_visits sv
                    ORDER BY sv.site_rank
                    """
                ).fetchall()
            )
        }
        for site in sites.values():
            self._populate_site_info(site, cursor)

        processed_path.write_bytes(pickle.dumps(sites))
        self.io_helper.logger.info(
            f"Processed {chunk_path.name} and cached results"
        )
        return sites

    def _parse_log_errors(self):
        self.errors: dict[str, str] = {}
        with open(self.io_helper.raw / f"{self.tranco_list_id}.log") as log:
            for line in log:
                if (m := neterror_pattern.search(line)) is not None:
                    gd = m.groupdict()
                    if gd['url'] in self.errors:
                        assert self.errors[gd['url']] == gd['error_type'], \
                            f"Multiple neterrors for site {gd['url']}"
                    self.errors[gd['url']] = gd['error_type']

    def _geterror(
        self,
        site: str
    ):
        return FirefoxCrawlFailureReason(self.errors.get(site, 'netTimeout'))

    def _populate_site_info(
        self,
        site: TrancoSiteCrawlResult,
        cursor: sqlite3.Cursor
    ):
        site.root_page_url = self._get_root_page_url(site, cursor)
        reqs = cursor.execute(
            f"""
            SELECT h.request_id, h.url, h.resource_type,
                   d.hostname, d.canonical_name, d.addresses, d.used_address
            FROM http_requests h
            INNER JOIN dns_responses d ON d.visit_id=h.visit_id AND d.request_id=h.request_id
            WHERE h.visit_id={site.visit_id} AND d.used_address IS NOT NULL
            ORDER BY h.request_id;
            """
        ).fetchall()

        if not reqs:
            site.failure_reason = self._geterror(site.fqdn.name)

        for req_raw in reqs:
            (request_id, url, resource_type,
             hostname, cname, addresses, used_address) = req_raw
            assert used_address is not None, "used_address is None"
            addresses: str
            dns_record = UsedDnsRecord(
                FQDN(hostname),
                FQDN(cname),
                tuple([ip_address(ip) for ip in addresses.split(',')]),
                ip_address(used_address),
            )
            site.http_requests.append(
                CompletedHttpRequest(
                    request_id,
                    url,
                    FirefoxWebResource(resource_type),
                    dns_record,
                )
            )

    def _get_root_page_url(
        self,
        site: TrancoSiteCrawlResult,
        cursor: sqlite3.Cursor
    ):
        """
        Try to find the URL of the actual root page with least effort
        """
        # Try 1: If we requested http://site/, use its request_id
        queryresp = cursor.execute(
            f"""
            SELECT request_id FROM http_requests
            WHERE visit_id={site.visit_id} AND url='{f"{site.url}/"}'
            """
        ).fetchone()

        if not queryresp:
            # Try 2: If we got a redirect for http://site/, use its request_id
            queryresp = cursor.execute(
                f"""
                SELECT old_request_id from http_redirects
                WHERE visit_id={site.visit_id} AND old_request_url='{f"{site.url}/"}'
                """
            ).fetchone()

        # Now look in the http_responses table for this request_id
        try:
            (root_reqid,) = queryresp
            root_page_url: str
            (root_page_url,) = cursor.execute(
                f"""
                SELECT url FROM http_responses
                WHERE visit_id={site.visit_id} AND request_id={root_reqid}
                """
            ).fetchone()
        except:
            # If that does not work, we have no other option but to walk
            root_page_url = self._get_root_page_url_walk(site, cursor)
        return root_page_url

    def _get_root_page_url_walk(
        self,
        site: TrancoSiteCrawlResult,
        cursor: sqlite3.Cursor
    ):
        """
        Walk the redirect chain starting at http://site/
        to find the URL of the actual root page
        """
        # Exit early if there are no HTTP responses
        resp_urls: set[str] = set(map(
            lambda x: x[0],
            cursor.execute(
                f"""
                SELECT url FROM http_responses
                WHERE visit_id={site.visit_id}
                """
            ).fetchall()
        ))
        if not resp_urls:
            return None

        redirects: set[str] = set()
        url = f"{site.url}/"
        while True:
            redirects.add(url)
            next_redir: list[str] = cursor.execute(
                f"""
                SELECT new_request_url FROM http_redirects
                WHERE old_request_url='{url}' AND visit_id={site.visit_id}
                """
            ).fetchall()
            if not next_redir:
                # No more redirects
                break
            # next_redir can have multiple entries;
            # it seems like the last one is our best bet
            (newurl,) = next_redir[-1]
            url = newurl
            if url in redirects:
                # Found a redirect loop
                break

        # Only the "real" redirect url would have an http_responses entry
        redirects_with_resp = resp_urls.intersection(redirects)
        if redirects_with_resp:
            # This would handle most cases
            assert len(redirects_with_resp) == 1
            return redirects_with_resp.pop()
        else:
            # If there is an http_response entry with the same domain as site
            # or its root page redirects, return that
            for url in resp_urls:
                for red_url in redirects:
                    if urlparse(url).netloc == urlparse(red_url).netloc:
                        return url
            # Out of luck
            self.io_helper.logger.warning(
                f"Could not find root page URL for {site.url}"
            )
            return None
