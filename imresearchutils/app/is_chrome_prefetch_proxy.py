import requests
import pandas as pd
import csv
import ipaddress


from imresearchutils.common import *

class ChromePrefetchUtil:
    """
    columns: cidr, country
    """

    CACHE_KB_DEFAULT = 256_000

    def __init__(self,
                 data_dir: Path | None = None,
                 **kwargs,
                 ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )
        data_url = f"https://www.gstatic.com/chrome/prefetchproxy/prefetch_proxy_geofeed"
        saved_data_file = self.io_helper.raw / data_url.split("/")[-1]

        # Check if the files exist, if not, download them
        for (url, saved_file) in [
            (data_url, saved_data_file),
        ]:
            if saved_file.exists():
                continue

            try:
                self.io_helper.logger.info(
                    f"Downloading Chrome Prefetch Proxy file from {url} to {saved_file}"
                )
                r = requests.get(url, timeout=5)
            except requests.exceptions.Timeout:
                self.io_helper.logger.error(
                    f"Could not download Chrome Prefetch Proxy file from {url}, cannot proceed"
                )
                raise
            else:
                saved_file.write_text(r.text)

        # Load raw data file & Parse the file
        self.df = self._load_geofeed_from_file(saved_data_file)

        self.io_helper.logger.info(
            f"Loaded Chrome Prefetch Proxy dataset from {saved_data_file}"
        )

        # building tree
        self.db_path = self.io_helper.processed / f"{saved_data_file.stem}.sqlite3"
        is_initialized = self.db_path.exists()

        # Initialize SqliteDatabase and register the table
        self.db = SqliteDatabase(
            self.db_path,
            logger=self.io_helper.logger,
        )
        self.ipinfo_carrier_table: SqliteTable = self.db.register_table(
            "chrome_prefetch",
            schema={
                "version": int,
                "prefix_length": int,
                "network_start": IPv6Address,
                "network_end": IPv6Address,

                "country": str,
            },
            qualifiers={
                "version": "NOT NULL",
                "prefix_length": "NOT NULL",
                "network_start": "NOT NULL",
                "network_end": "NOT NULL",
            },
            table_constraints=[
                "PRIMARY KEY (version, network_start, prefix_length)"
            ],
            indices=[["version", "network_start", "network_end"]],
        )
        if not is_initialized:
            self._populate_table(self.df)

        # Use SqliteLpmTrieHelper for fast lookups
        self.lpm_helper = SqliteLpmTrieHelper(
            self.db.path,
            self.ipinfo_carrier_table,
            logger=self.io_helper.logger,
        )

        self.io_helper.logger.info(
            f"Initialized IpInfoCarrierUtil with module directory: {self.io_helper.module_dir}"
        )

    def _populate_table(self, df: pd.DataFrame):
        # Stream the CSV in chunks, compute numeric columns, insert
        self.io_helper.logger.info(
            f"Populating SQLite database at {self.db_path}"
        )
        df_chunk = df

        net_objs = df_chunk.pop("network").map(lambda x: ip_network(x))
        df_chunk["version"] = net_objs.map(lambda n: n.version)
        df_chunk["prefix_length"] = net_objs.map(lambda n: n.prefixlen)
        df_chunk["network_start"] = net_objs.map(
            lambda n: n.network_address
        )
        df_chunk["network_end"] = net_objs.map(
            lambda n: n.broadcast_address
        )

        # Write to the SQLite database
        self.ipinfo_carrier_table.insert_df(df_chunk)

        self.io_helper.logger.info(
            "Created and populated SQLite database at {self.db_path}."
        )

    def _load_geofeed_from_file(self, path: Path) -> pd.DataFrame:
        with path.open("r", encoding="utf-8") as f:
            lines = [
                line.strip() for line in f
                if line.strip() and not line.startswith("#")
            ]

        records = [line.split(",") for line in lines]
        df = pd.DataFrame(records, columns=['network', 'country', 'field1', 'field2', 'field3'])

        # if empty, drop the columns
        empty_cols = [col for col in ['field1', 'field2', 'field3']
                      if df[col].str.strip().eq('').all()]
        df = df.drop(columns=empty_cols)

        return df

    def lookup(
            self,
            ip: IPv4Address | IPv6Address | str
    ) -> pd.Series:

        """
        Lookup the carrier info for a given IP.

        Args:
            addr (IPv4Address | IPv6Address | str):
                The IP address to look up.

        Returns:
            ret (pd.Series | None):
                A pandas Series containing the privacy information for the given IP address,
                or None if the address is not found in the dataset.
                In original dataset, the columns are:
                    - network, country
        """
        return self.lpm_helper.lookup(ip)
