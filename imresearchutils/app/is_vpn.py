from imresearchutils.common import *
import pandas as pd
import requests
import sqlite3
from pytricia import PyTricia


class VpnIpAz0:
    """
        Utility class for interacting with the az0/vpn_ip Github library.

        Args:
            data_dir (Path | None):
                Base directory for data files.
                If None, the current working directory will be used.

            **kwargs (dict):
                Additional arguments for IOHelper.
                See the IOHelper class for more details.
    """
    URL_IP = "https://raw.githubusercontent.com/az0/vpn_ip/refs/heads/main/data/output/ip.txt"
    URL_HOSTNAME = "https://raw.githubusercontent.com/az0/vpn_ip/refs/heads/main/data/output/hostname.txt"

    def __init__(
        self,
        data_dir: Path | None = None,
        **kwargs,
    ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )

        self._download_current_file()

        self.io_helper.logger.info(
            f"Initialized VpnIPAz0 with raw directory: {self.io_helper.raw}"
        )

    def _download_current_file(self):
        """
        Downloads the latest VPN IP and hostname data from the az0/vpn_ip repository.
        This method retrieves the latest data files and saves them in the raw directory, and create dataframe and return.
        """

        ip_file = self.io_helper.raw / "vpn_ip.txt"
        hostname_file = self.io_helper.raw / "vpn_hostname.txt"

        try:
            self.io_helper.logger.info(
                "Downloading current VPN IP and hostname data..."
            )
            r_ip = requests.get(self.URL_IP, timeout=5)
            r_hostname = requests.get(self.URL_HOSTNAME, timeout=5)
        except requests.exceptions.Timeout:
            self.io_helper.logger.error(
                f"Could not download VPN IP data from {self.URL_IP}, cannot proceed"
            )
            raise
        else:
            ip_file.write_text(r_ip.text)
            hostname_file.write_text(r_hostname.text)

        # Convert results into DataFrames
        self.df_vpn_hostname = pd.read_csv(
            hostname_file,
            header=None,
            comment="#",
            names=["hostname"],
            dtype={'hostname': str},
        )

        self.df_vpn_ip = pd.read_csv(
            ip_file,
            sep=r'\s*#\s*',
            engine='python',
            header=None,
            names=["ip", "hostname"],
            dtype={"ip": str, "hostname": str},
            skip_blank_lines=True,
        )

    def get_hostnames(self):
        """
        Returns a list of hostnames from the az0/vpn_ip dataset.
        """
        return self.df_vpn_hostname

    def get_vpn_ips(self) -> list[tuple[str | IPv4Address | IPv6Address, str]]:
        """
        Returns a list of VPN IPs from the az0/vpn_ip dataset.
        """
        return self.df_vpn_ip

    def is_hostname_vpn(self, hostname: str) -> bool:
        """
        Checks if the given hostname is associated with a VPN IP.

        Args:
            hostname (str): The hostname to check.

        Returns:
            is_vpn (bool): True if the hostname is associated with a VPN IP, False otherwise.
        """

        # Normalize input
        host = hostname.strip().lower()

        # Ensure df exists and has the expected column
        if not hasattr(self, 'df_vpn_hostname') or 'hostname' not in self.df_vpn_hostname:
            return False

        # Build a lowercase set of known VPN hostnames for fast lookup
        vpn_hosts = set(self.df_vpn_hostname['hostname'])

        return host in vpn_hosts

    def is_ip_vpn(self, ip: str | IPv4Address | IPv6Address) -> tuple[bool, Optional[str]]:
        """
        Checks if the given IP address is a VPN IP.

        Args:
            ip (IPv4Address | IPv6Address): The IP address to check.

        Returns:
            (is_vpn, hostname) (tuple[bool, Optional[str]]):
            A tuple where the first element is True if the IP is a VPN IP,
            and the second element is the hostname if available, otherwise None.
        """

        # Convert IP object to string for comparison
        ip_str = str(ip)

        # Filter df_vpn_ip for matching ip
        match = self.df_vpn_ip[self.df_vpn_ip['ip'].astype(str) == ip_str]

        if not match.empty:
            # If found, take the first hostname
            hostname = match.iloc[0]['hostname']
            return True, hostname

        return False, None


class ListsVpnX4BNet:
    """
    Utility class for interacting with the X4BNet/lists_vpn VPN and datacenter lists.

    Downloads and caches the IPv4 prefix lists for both VPN-only and
    combined datacenter+VPN networks, and provides query methods.

    Args:
            data_dir (Path | None):
                Base directory for data files.
                If None, uses the current working directory.

            **kwargs:
                Additional arguments for IOHelper.
    """
    URL_VPN_IPV4 = (
        "https://raw.githubusercontent.com/X4BNet/lists_vpn/"
        "main/output/vpn/ipv4.txt"
    )
    URL_DC_IPV4 = (
        "https://raw.githubusercontent.com/X4BNet/lists_vpn/"
        "main/output/datacenter/ipv4.txt"
    )

    def __init__(
        self,
        data_dir: Path | None = None,
        **kwargs,
    ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs
        )

        # Download raw files and load into DataFrames
        self._download_lists()

        # Load into DataFrames
        self._load_dataframes()

        self.io_helper.logger.info(
            f"Initialized ListsVpnX4BNet with raw directory: {self.io_helper.raw}"
        )

    def _download_lists(self) -> None:
        """
        Download the latest VPN-only and datacenter+VPN IPv4 lists
        into the raw directory.
        Load the downloaded text files into pandas DataFrames.
        Each file is a one-column list of prefixes (CIDRs).
        """
        vpn_v4_file = self.io_helper.raw / "vpn_ipv4.txt"
        dc_v4_file = self.io_helper.raw / "dc_ipv4.txt"

        try:
            self.io_helper.logger.info(
                "Downloading VPN and datacenter IPv4 lists..."
            )
            r_vpn = requests.get(self.URL_VPN_IPV4, timeout=5)
            r_dc = requests.get(self.URL_DC_IPV4, timeout=5)
        except requests.exceptions.Timeout:
            self.io_helper.logger.error(
                f"Could not download VPN IPv4 data from {self.URL_VPN_IPV4}, cannot proceed"
            )
            raise
        else:
            vpn_v4_file.write_text(r_vpn.text)
            dc_v4_file.write_text(r_dc.text)

        self.df_vpn = pd.read_csv(
            vpn_v4_file,
            header=None,
            names=["prefix"],
            dtype={"prefix": str},
            comment="#",
        )

        self.df_dc = pd.read_csv(
            dc_v4_file,
            header=None,
            names=["prefix"],
            dtype={"prefix": str},
            comment="#",
        )

    def get_vpn_ips(self) -> pd.DataFrame:
        """
        Returns:
            pd.DataFrame: DataFrame of VPN-only prefixes (column: prefix).
        """
        return self.df_vpn.copy()

    def get_datacenter_ips(self) -> pd.DataFrame:
        """
        Returns:
            pd.DataFrame: DataFrame of datacenter+VPN prefixes (column: prefix).
        """
        return self.df_dc.copy()

    def is_ip_vpn(
        self,
        ip: str | IPv4Address,
    ) -> tuple[bool, Optional[str]]:
        """
        Checks if the given IPv4 address falls within any VPN-only prefix.

        Args:
            ip (str | IPv4Address):
                The IP address to check.
        Returns:
            (is_vpn, prefix) (tuple(bool, Optional[str])):
                True and matching prefix if VPN; else False, None.
        """
        ip_obj = ip_address(str(ip))
        # Iterate over prefixes
        for pref in self.df_vpn["prefix"]:
            network = ip_network(pref)
            if ip_obj in network:
                return True, pref
        return False, None

    def is_ip_datacenter(
        self,
        ip: str | IPv4Address,
    ) -> tuple[bool, Optional[str]]:
        """
        Checks if the given IPv4 address falls within any datacenter+VPN prefix.

        Args:
            ip (str | IPv4Address): The IP address to check.
        Returns:
            (bool, Optional[str]): True and matching prefix if datacenter/VPN; else False, None.
        """
        ip_obj = ip_address(str(ip))
        for pref in self.df_dc["prefix"]:
            network = ip_network(pref)
            if ip_obj in network:
                return True, pref
        return False, None


class IpInfoPrivacyUtil:
    """
    Utility class for interacting with the ipinfo.io privacy dataset.

    Args:
        ipinfo_privacy_dir (Path):
            Directory where the ipinfo privacy dataset is stored.

        date (str | None):
            Date of the dataset to use, in 'YYYY-MM-DD' format.
            If None, the latest available dataset will be used.

        data_dir (Path | None):
            Base directory for data files.
            If None, the current working directory will be used.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    CSV_CHUNK_SIZE = 50_000

    def __init__(
        self,
        ipinfo_privacy_dir: Path,
        date: str | None = None,
        data_dir: Path | None = None,
        **kwargs,
    ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            raw_dir_symlink_to=ipinfo_privacy_dir,
            data_dir=data_dir,
            **kwargs,
        )
        self._init_db(date)
        self.io_helper.logger.info(
            f"Initialized IpInfoPrivacyUtil with raw directory: {self.io_helper.raw}"
        )

    def _init_db(self, date: str | None = None):
        if date is not None:
            # Verify that the date is in the ISO format 'YYYY-MM-DD'
            try:
                pd.to_datetime(date, format='%Y-%m-%d', errors='raise')
            except ValueError:
                self.io_helper.logger.error(
                    f"Invalid date format: {date}. Expected 'YYYY-MM-DD'."
                )
                raise

            # Verify that the corresponding file exists
            raw_path = self.io_helper.raw / f"ipinfo_privacy.{date}.csv"
            if not raw_path.exists():
                self.io_helper.logger.error(
                    f"Data file for date {date} does not exist: {raw_path}"
                )
                raise FileNotFoundError(
                    f"Data file for date {date} not found.")
        else:
            # List available data files
            data_files = list(self.io_helper.raw.glob("ipinfo_privacy.*.csv"))
            if not data_files:
                self.io_helper.logger.error(
                    "No ipinfo privacy data files found in the raw directory."
                )
                raise FileNotFoundError("No ipinfo privacy data files found.")

            # Sort by date and take the most recent one
            data_files.sort(key=lambda x: x.stem.split('.')[-1], reverse=True)
            raw_path = data_files[0]

        # Initialize and open the database
        self.db_path = self.io_helper.processed / f"{raw_path.stem}.sqlite3"
        is_initialized = self.db_path.exists()
        self.db_conn = sqlite3.connect(self.db_path)
        self.db_conn.execute("PRAGMA journal_mode=WAL;")  # Write-Ahead Logging

        if is_initialized:
            self.io_helper.logger.info(
                f"Database already exists at {self.db_path}, skipping initialization."
            )
            return

        # Create the table
        self.db_conn.execute("""
        CREATE TABLE ipinfo_privacy (
            version           INTEGER    NOT NULL,
            prefix_length     INTEGER    NOT NULL,
            network_start     BLOB       NOT NULL,
            network_end       BLOB       NOT NULL,
            hosting           BOOLEAN,
            proxy             BOOLEAN,
            tor               BOOLEAN,
            relay             BOOLEAN,
            vpn               BOOLEAN,
            service           TEXT,
            PRIMARY KEY (version, network_start, prefix_length)
        );
        """)

        # Index network range for faster lookups
        self.db_conn.execute("""
        CREATE INDEX idx_version_range ON ipinfo_privacy (
            version, network_start, network_end
        );
        """)

        # Stream the CSV in chunks, compute numeric columns, insert
        chunker = pd.read_csv(
            raw_path,
            dtype={
                "hosting": bool,
                "proxy":   bool,
                "tor":     bool,
                "relay":   bool,
                "vpn":     bool,
            },
            chunksize=self.CSV_CHUNK_SIZE,
        )
        for df_chunk in chunker:
            df_chunk: pd.DataFrame

            net_objs = df_chunk.pop("network").map(lambda x: ip_network(x))
            df_chunk["version"] = net_objs.map(lambda n: n.version)
            df_chunk["prefix_length"] = net_objs.map(lambda n: n.prefixlen)
            df_chunk["network_start"] = net_objs.map(
                lambda n: n.network_address.packed
            )
            df_chunk["network_end"] = net_objs.map(
                lambda n: n.broadcast_address.packed
            )

            # Append to the database
            df_chunk.to_sql(
                "ipinfo_privacy",
                self.db_conn,
                if_exists="append",
                index=False,
            )

        self.db_conn.commit()

        self.io_helper.logger.info(
            "Created and populated SQLite database at {self.db_path}."
        )

    def lookup(
        self,
        addr: IPv4Address | IPv6Address | str,
    ) -> pd.Series | None:
        """
        Lookup the privacy information for a given IP address.

        Args:
            addr (IPv4Address | IPv6Address | str):
                The IP address to look up.

        Returns:
            ret (pd.Series | None):
                A pandas Series containing the privacy information for the given IP address,
                or None if the address is not found in the dataset.
        """

        ip = ip_address(addr) if isinstance(addr, str) else addr
        ip_blob = ip.packed

        sql = """
            SELECT
                version, network_start, prefix_length,
                hosting, proxy, tor, relay, vpn, service
            FROM ipinfo_privacy
            WHERE version = ?
            AND network_start <= ?
            AND network_end   >= ?
            ORDER BY prefix_length DESC
            LIMIT 1
        """
        cur = self.db_conn.execute(sql, (ip.version, ip_blob, ip_blob))
        row = cur.fetchone()
        if row is None:
            return None

        (version, start_blob, prefix_length,
         hosting, proxy, tor, relay, vpn, service) = row

        start_int = int.from_bytes(start_blob, byteorder="big")
        net = ip_network((start_int, prefix_length))

        return pd.Series({
            "version":        version,
            "prefix_length":  prefix_length,
            "network":        str(net),
            "network_start":  net.network_address,
            "network_end":    net.broadcast_address,
            "service":        service,
            "hosting":        bool(hosting),
            "proxy":          bool(proxy),
            "tor":            bool(tor),
            "relay":          bool(relay),
            "vpn":            bool(vpn),
        })
