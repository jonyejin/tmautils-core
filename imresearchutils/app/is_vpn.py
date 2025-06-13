from imresearchutils.common import *
import pandas as pd
import requests


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

        self._download_current()

        self.io_helper.logger.info(
            f"Initialized VpnIPAz0 with cache directory: {self.io_helper.processed}"
        )

    def _download_current(self):
        """
        Downloads the latest VPN IP and hostname data from the az0/vpn_ip repository.
        This method retrieves the latest data files and saves them in the processed directory, and create dataframe and return.
        """

        ip_file = self.io_helper.processed / "vpn_ip.txt"
        hostname_file = self.io_helper.processed / "vpn_hostname.txt"

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

# TODO


class ListsVpnX4BNet:
    """
    Utility class for interacting with the x4b.net VPN lists.
    """

    def __init__(self):
        pass

    def _load_cache(self):
        pass

    def download_current_file(self):
        pass

    def get_asns(self):
        """
        Returns a list of ASNs from the X4BNet/lists_vpn dataset.
        """
        pass

    def get_vpn_ips(self):
        """
        Returns a list of VPN IPs from the X4BNet/lists_vpn dataset.
        """
        pass

    def is_ip_vpn(self, ip: IPv4Address | IPv6Address) -> tuple[bool, Optional[str]]:
        """
        Checks if the given IP address is a VPN IP.

        Args:
            ip (IPv4Address | IPv6Address): The IP address to check.

        Returns:
            tuple[bool, Optional[str]]: A tuple where the first element is True if the IP is a VPN IP,
                                        and the second element is the hostname if available, otherwise None.
        """
        pass
