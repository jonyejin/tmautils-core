from imresearchutils.common import *
import pandas as pd
import ipaddress

class IpInfoCarrierUtil:
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

        self._load_data(date)

        self.io_helper.logger.info(
            f"Initialized IpInfoCarrierUtil with raw directory: {self.io_helper.raw}"
        )

    def _load_data(self, date: str | None = None):
        """
        Load the data from the specified date.
        :param date: Date string in 'YYYY-MM-DD' format. If None, uses the latest data.
        """
        if date is None:
            date = self.io_helper.get_latest_date()

        self.io_helper.logger.info(f"Loading data for date: {date}")
        df = self.io_helper.load_data(date)

        if df is None:
            raise ValueError(f"No data found for date: {date}")
        
        # Convert network strings to ip_network objects for fast matching
        df["network"] = df["network"].apply(ipaddress.ip_network)
        self.data = df

    def is_ip_carrier(self, ip: str) -> bool:
        """
        Check if the given IP address is in the carrier IP prefix list.
        :param ip: IP address to check.
        :return: True if the IP is a carrier IP, False otherwise.
        """
        try:
            ip_obj = ipaddress.ip_address(ip)
            for network in self.data["network"]:
                if ip_obj in network:
                    return True
            return False
        except ValueError:
            self.io_helper.logger.warning(f"Invalid IP address format: {ip}")
            return False

    def get_carrier_info(self, ip: str) -> dict | None:
        """
        Return the carrier info for a given IP, if available.
        :param ip: IP address to check.
        :return: Dictionary of carrier info, or None if not found.
        """
        try:
            ip_obj = ipaddress.ip_address(ip)
            for _, row in self.data.iterrows():
                if ip_obj in row["network"]:
                    return {
                        "network": str(row["network"]),
                        "name": row["name"],
                        "country": row["country"],
                        "mcc": row["mcc"],
                        "mnc": row["mnc"],
                    }
            return None
        except ValueError:
            self.io_helper.logger.warning(f"Invalid IP address format: {ip}")
            return None
