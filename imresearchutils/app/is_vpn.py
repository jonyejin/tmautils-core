from imresearchutils.common import *
import pandas as pd
import urllib.request
import ipaddress

class VpnIpAz0:
    """
        Utility class for interacting with the az0/vpn_ip Github library.

        Args:
            vpn_ip_data_dir (Path):
                Directory where the VPN IP data is stored.
            
            data_dir (Path | None):
                Base directory for data files.
                If None, the current working directory will be used.

            **kwargs (dict):
                Additional arguments for IOHelper.
                See the IOHelper class for more details.
    """
    URL_IP = "https://raw.githubusercontent.com/az0/vpn_ip/refs/heads/main/data/output/ip.txt"
    URL_HOSTNAME = "https://raw.githubusercontent.com/az0/vpn_ip/refs/heads/main/data/output/hostname.txt"

    def __init__(self, 
                 data_dir: Path | None = None,
                 **kwargs):
        
        """
        Initializes the VpnIpAz0 utility class.
        Args:
            data_dir (Path | None): Base directory for data files. If None, the current working directory will be used.
            **kwargs (dict): Additional arguments for IOHelper.
        """
        
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )
        self._download_current_file()


    def _download_current_file(self):
        """
        Downloads the latest VPN IP and hostname data from the az0/vpn_ip repository.
        This method retrieves the latest data files and saves them in the raw directory, and create dataframe and return.
        """

        self.io_helper.logger.info("Downloading current VPN IP and hostname data...")
        urllib.request.urlretrieve(self.URL_IP, self.io_helper.raw / "vpn_ip.txt")
        urllib.request.urlretrieve(self.URL_HOSTNAME, self.io_helper.raw / "vpn_hostname.txt")
    
        # Convert the result into a DataFrame
        self.df_vpn_hostname = pd.read_csv(
            self.io_helper.raw / "vpn_hostname.txt",
            header=None,
            comment="#",
            names=["hostname"],
            dtype={'hostname': str},
        )

        self.df_vpn_ip = pd.read_csv(
            self.io_helper.raw / "vpn_ip.txt",
            sep=r'\s*#\s*',
            engine='python',
            header=None,
            names=["ip", "hostname"],
            dtype={"ip": str, "hostname": str},
            skip_blank_lines=True
        )
        return self.df_vpn_ip, self.df_vpn_hostname



    def get_hostnames(self):
        """
        Returns a list of hostnames from the az0/vpn_ip dataset.
        """
        return self.df_vpn_hostname


    def get_vpn_ips(self) -> list[tuple[str|IPv4Address|IPv6Address, str]]:
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
            bool: True if the hostname is associated with a VPN IP, False otherwise.
        """

        # Normalize input
        host = hostname.strip().lower()

        # Ensure df exists and has the expected column
        if not hasattr(self, 'df_vpn_hostname') or 'hostname' not in self.df_vpn_hostname:
            return False

        # Build a lowercase set of known VPN hostnames for fast lookup
        vpn_hosts = set(self.df_vpn_hostname['hostname'])

        return host in vpn_hosts
        

    def is_ip_vpn(self, ip: str|IPv4Address | IPv6Address) -> tuple[bool, Optional[str]]:
        """
        Checks if the given IP address is a VPN IP.

        Args:
            ip (IPv4Address | IPv6Address): The IP address to check.

        Returns:
            tuple[bool, Optional[str]]: A tuple where the first element is True if the IP is a VPN IP,
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
        **kwargs
    ):
        """
        Args:
            data_dir (Path | None): Base directory for data files.
                If None, uses the current working directory.
            **kwargs: Additional arguments for IOHelper.
        """
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs
        )
        # Ensure storage directories
        self.io_helper.raw.mkdir(parents=True, exist_ok=True)
        self.io_helper.processed.mkdir(parents=True, exist_ok=True)

        # Download raw files
        self._download_lists()
        # Load into DataFrames
        self._load_dataframes()

    def _download_lists(self) -> None:
        """
        Download the latest VPN-only and datacenter+VPN IPv4 lists
        into the raw directory.
        """
        raw = self.io_helper.raw
        urllib.request.urlretrieve(
            self.URL_VPN_IPV4, raw / "vpn_ipv4.txt"
        )
        urllib.request.urlretrieve(
            self.URL_DC_IPV4, raw / "dc_ipv4.txt"
        )

    def _load_dataframes(self) -> None:
        """
        Load the downloaded text files into pandas DataFrames.
        Each file is a one-column list of prefixes (CIDRs).
        """
        raw = self.io_helper.raw
        # VPN DataFrame
        self.df_vpn = pd.read_csv(
            raw / "vpn_ipv4.txt",
            header=None,
            names=["prefix"],
            dtype={"prefix": str},
            comment="#",
        )
        # Datacenter+VPN DataFrame
        self.df_dc = pd.read_csv(
            raw / "dc_ipv4.txt",
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
        ip: str | ipaddress.IPv4Address,
    ) -> tuple[bool, Optional[str]]:
        """
        Checks if the given IPv4 address falls within any VPN-only prefix.

        Args:
            ip (str | IPv4Address): The IP address to check.
        Returns:
            (bool, Optional[str]): True and matching prefix if VPN; else False, None.
        """
        ip_obj = ipaddress.ip_address(str(ip))
        # Iterate over prefixes
        for pref in self.df_vpn["prefix"]:
            network = ipaddress.ip_network(pref)
            if ip_obj in network:
                return True, pref
        return False, None

    def is_ip_datacenter(
        self,
        ip: str | ipaddress.IPv4Address,
    ) -> tuple[bool, Optional[str]]:
        """
        Checks if the given IPv4 address falls within any datacenter+VPN prefix.

        Args:
            ip (str | IPv4Address): The IP address to check.
        Returns:
            (bool, Optional[str]): True and matching prefix if datacenter/VPN; else False, None.
        """
        ip_obj = ipaddress.ip_address(str(ip))
        for pref in self.df_dc["prefix"]:
            network = ipaddress.ip_network(pref)
            if ip_obj in network:
                return True, pref
        return False, None
