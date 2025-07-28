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
            f"Initialized IpInfoCarrierUtil with module directory: {self.io_helper.module_dir}"
        )

    def _load_data(self, date: str | None = None):
        """
        Load the carrier IP data for a specific date (or the latest one available).
        """
        from ipaddress import ip_network

        if date is not None:
            try:
                pd.to_datetime(date, format='%Y-%m-%d', errors='raise')
            except ValueError:
                self.io_helper.logger.error(
                    f"Invalid date format: {date}. Expected 'YYYY-MM-DD'."
                )
                raise

            raw_path = self.io_helper.raw / f"ipinfo_carrier.{date}.csv"
            if not raw_path.exists():
                self.io_helper.logger.error(
                    f"Data file for date {date} not found: {raw_path}"
                )
                raise FileNotFoundError(
                    f"Data file for date {date} not found."
                )
        else:
            data_files = list(self.io_helper.raw.glob("ipinfo_carrier.*.csv"))
            if not data_files:
                self.io_helper.logger.error(
                    "No ipinfo carrier data files found."
                )
                raise FileNotFoundError("No ipinfo carrier data files found.")

            data_files.sort(key=lambda f: f.stem.split('.')[-1], reverse=True)
            raw_path = data_files[0]
            date = raw_path.stem.split('.')[-1]

        self.io_helper.logger.info(f"Loading carrier IP data from: {raw_path}")

        # Load CSV
        df = pd.read_csv(
            raw_path,
            dtype={
                "name": "string",
                "country": "string",
                "mcc": "string",
                "mnc": "string",
            }
        )

        df["network"] = df["network"].apply(ip_network)

        self.data = df
        self.io_helper.logger.info(
            f"Loaded {len(df)} carrier prefixes for date: {date}"
        )

    def is_carrier(self, ip: str) -> bool:
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

    def get_carrier_by_ip(self, ip: str) -> dict | None:
        """
        Return the carrier info for a given IP, if available.

        Why?
            Sometimes we need to know the carrier information for a specific IP address.

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
