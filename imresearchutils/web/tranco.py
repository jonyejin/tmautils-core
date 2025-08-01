import sqlite3
import re
from urllib.parse import urlparse

from imresearchutils.common import *
from .types import *
from .openwpm import OpenWpmCrawlUtil

neterror_pattern = re.compile(
    r"Received neterror (?P<error_type>\w+) while executing command: "
    r"BrowseCommand\(http://(?P<url>.*),5,3\)"
)


class TrancoTopListUtil:
    def __init__(
        self,
        date: str | None = None,
        list_id: str | None = None,
        subdomains: bool = False,
        full: bool = False,
        data_dir: Path | None = None,
        n_top_sites: int = 100000,
        **kwargs,
    ):
        import tranco

        self.n_top_sites = n_top_sites

        self.io_helper = IOHelper(
            module_name=self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )

        self.tranco_list = tranco.Tranco(
            cache_dir=self.io_helper.raw,
        ).list(
            date=date,
            list_id=list_id,
            subdomains=subdomains,
            full=full,
        )

        self.io_helper.logger.info(
            f"Obtained Tranco list with id {self.tranco_list.list_id} "
            f"and date {self.tranco_list.date}"
        )

    @property
    def list(self):
        return [f"http://{x}" for x in self.tranco_list.top(self.n_top_sites)]

    @property
    def list_id(self):
        return self.tranco_list.list_id


class PeriodicTrancoCrawlUtil:
    """
    A utility class for periodically crawling the Tranco top list using OpenWPM.

    Args:
        openwpm_path (Path):
            Path to the OpenWPM installation directory.

        data_dir (Path | None):
            Base directory for data files.
            If None, the current working directory will be used.

        continue_only (bool):
            If True, only resume incomplete crawls.
            If False, resume incomplete crawls and start a new one.
            Default is True.

        top_count (int):
            Number of top sites to crawl from the Tranco list.
            Default is 100,000.

        openwpm_args (dict):
            Additional arguments to pass to the OpenWPM crawl utility.
            See the OpenWpmCrawlUtil class for more details.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    def __init__(
        self,
        openwpm_path: Path,
        data_dir: Path | None = None,
        continue_only: bool = True,
        top_count: int = 100000,
        openwpm_args: dict = {},
        **kwargs,
    ):
        self.openwpm_path = openwpm_path
        self.openwpm_args = openwpm_args
        self.data_dir = data_dir
        self.top_count = top_count

        self.io_helper = IOHelper(
            module_name=self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )

        # Resume incomplete crawls
        self.resume_crawls()

        # If not continuing, start a new crawl
        if not continue_only:
            self.crawl()

    def crawl(self):
        """
        Start a new crawl using the latest Tranco top list.

        This method fetches the latest Tranco list, merges it with any previous
        crawls, and initiates an OpenWPM crawl with the combined list.
        """

        from datetime import datetime

        self.io_helper.logger.info("Starting new crawl")

        prev_sites = []

        # Start with old list of sites if a previous crawl exists
        openwpm_dir = self.data_dir / OpenWpmCrawlUtil.__name__
        if openwpm_dir.exists():
            prev_crawls = sorted(
                [d for d in openwpm_dir.glob("*") if d.is_dir()],
                key=lambda d: d.name,
            )
            if prev_crawls:
                last_crawl = prev_crawls[-1]
                prev_util = OpenWpmCrawlUtil(
                    openwpm_path=self.openwpm_path,
                    crawl_id=last_crawl.name,
                    data_dir=self.data_dir,
                    **self.openwpm_args,
                )
                prev_sites = prev_util.sites
                self.io_helper.logger.info(
                    f"Starting with {len(prev_sites)} sites from last crawl"
                )

        # Fetch the latest Tranco top list
        tranco_util = TrancoTopListUtil(
            data_dir=self.data_dir,
            n_top_sites=self.top_count,
        )

        # Merge previous sites with the latest Tranco list
        seen = set(prev_sites)
        merged = prev_sites + [s for s in tranco_util.list if s not in seen]
        self.io_helper.logger.info(
            f"Total sites to crawl: {len(merged)} "
            f"(including {len(prev_sites)} from previous crawls)"
        )

        # Create OpenWPM crawl utility with merged sites
        openwpm_util = OpenWpmCrawlUtil(
            openwpm_path=self.openwpm_path,
            crawl_id=datetime.now().date().isoformat(),
            sites=merged,
            data_dir=self.data_dir,
            **self.openwpm_args,
        )

        # Write metadata about the crawl
        openwpm_util.io_helper.raw.joinpath("tranco_list_id.txt").write_text(
            tranco_util.list_id
        )
        openwpm_util.io_helper.raw.joinpath("tranco_list.txt").write_text(
            "\n".join(tranco_util.list) + "\n"
        )

        # Start the crawl
        self.io_helper.logger.info(
            f"Starting OpenWPM crawl with id {openwpm_util.crawl_id} "
            f"and {len(openwpm_util.sites)} sites."
        )
        openwpm_util.crawl()
        self.io_helper.logger.info(
            f"Crawl completed for {openwpm_util.crawl_id} with "
            f"{len(openwpm_util.sites)} sites."
        )

    def resume_crawls(self):
        """
        Resume any incomplete OpenWPM crawls from previous runs.
        """

        self.io_helper.logger.info("Resuming incomplete crawls")

        openwpm_dir = self.data_dir / OpenWpmCrawlUtil.__name__
        if not openwpm_dir.exists():
            # No previous crawls to resume
            self.io_helper.logger.info(
                "No previous OpenWPM crawls found, nothing to resume."
            )
            return

        crawl_dirs = [d for d in openwpm_dir.glob("*") if d.is_dir()]
        for crawl_dir in crawl_dirs:
            openwpm_util = OpenWpmCrawlUtil(
                openwpm_path=self.openwpm_path,
                crawl_id=crawl_dir.name,
                data_dir=self.data_dir,
                **self.openwpm_args,
            )

            if openwpm_util.is_crawl_done():
                self.io_helper.logger.info(
                    f"Crawl for {crawl_dir.name} is already done."
                )
                continue

            self.io_helper.logger.info(
                f"Resuming crawl for {openwpm_util.crawl_id} with "
                f"{len(openwpm_util.sites)} sites."
            )

            openwpm_util.crawl()

            self.io_helper.logger.info(
                f"Crawl for {crawl_dir.name} completed."
            )


class TrancoProcessUtil:
    """
    A utility class for processing the results of a Tranco crawl.

    Args:
        tranco_list_id (str):
            ID of the Tranco list to process.

        data_dir (Path | None):
            Base directory for data files.
            If None, the current working directory will be used.

        repeat (int):
            Repetition number for the crawl.
            Used to create a unique instance name for the IOHelper.
            Default is 1.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    def __init__(
        self,
        tranco_list_id: str,
        data_dir: Path | None = None,
        repeat: int = 1,
        **kwargs,
    ):
        self.tranco_list_id = tranco_list_id

        # Reuse the data directory from the crawl
        instance_name = (
            self.tranco_list_id if repeat == 1 else f"{self.tranco_list_id}_{repeat}"
        )
        self.io_helper = IOHelper(
            module_name=self.__class__.__name__,
            instance_name=instance_name,
            data_dir=data_dir,
            **kwargs,
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

    def get_db_cursor(self, chunk_num: int):
        db_path = self.io_helper.raw / f"crawl_chunk_{chunk_num}.sqlite"
        if not db_path.exists():
            raise ValueError(
                f"Chunk {chunk_num} does not exist in {self.io_helper.raw}"
            )

        return sqlite3.connect(db_path).cursor()

    def load_db(
        self,
        chunk_num: int,
        use_cache: bool = True
    ):
        """
        Load the database for a specific chunk.

        Args:
            chunk_num (int):
                Chunk number to load.

            use_cache (bool):
                Whether to use the cached version of the database, if available.
                Default is True.
        """

        import pickle

        # Check if chunk_num is valid
        chunk_path = self.io_helper.raw / f"crawl_chunk_{chunk_num}.sqlite"
        if not chunk_path.exists():
            raise ValueError(
                f"Chunk {chunk_num} does not exist"
            )

        sites: dict[FQDN, OpenWpmSiteCrawlResult] = None
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
        cursor = self.get_db_cursor(chunk_num)
        sites = {
            s.fqdn: s for s in map(
                lambda x: OpenWpmSiteCrawlResult(*x),
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
        with open(self.io_helper.raw / "openwpm.log") as log:
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
        site: OpenWpmSiteCrawlResult,
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
        site: OpenWpmSiteCrawlResult,
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
        site: OpenWpmSiteCrawlResult,
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
