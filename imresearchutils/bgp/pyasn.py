from ftplib import FTP

from pyasn import pyasn, mrtx

from imresearchutils.common import *


ROUTEVIEWS_FTP_SERVER = "archive.routeviews.org"
ROUTEVIEWS_ARCHIVE_ROOT = "route-views4/bgpdata"  # IPv4 + IPv6


class PyasnUtil:
    """
    Utility class for interacting with the pyasn database.

    Args:
        year (int):
            Year of the RIB database to download.

        month (int):
            Month of the RIB database to download.

        day (int):
            Day of the RIB database to download.
            Default is 1.

        data_dir (Path | None):
            Base directory for data files.
            If None, the current working directory will be used.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    def __init__(
        self,
        year: int,
        month: int,
        day: int = 1,
        data_dir: Path | None = None,
        **kwargs,
    ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )

        # We don't know which hour we downloaded
        db_path = next(
            self.io_helper.processed.glob(
                f"rib.{year}{month:02d}{day:02d}.*.processed"
            ),
            None
        )

        if db_path is None:
            self.io_helper.logger.warning(
                f"pyasn database not found for {year}-{month:02d}-{day:02d}, "
                f"attempting to download"
            )
            db_path = self._download_and_process_rib(year, month, day)

        self.as_db = pyasn(str(db_path))

        self.io_helper.logger.info(
            f"Loaded pyasn database from {str(db_path)}"
        )

    def _download_and_process_rib(
        self,
        year: int,
        month: int,
        day: int,
    ):
        # Set up the FTP connection
        ftp = FTP(ROUTEVIEWS_FTP_SERVER)
        ftp.login()
        ftp.cwd(f"{ROUTEVIEWS_ARCHIVE_ROOT}/{year}.{month:02d}/RIBS")
        self.io_helper.logger.info(
            f"Set up FTP connection to {ROUTEVIEWS_FTP_SERVER}{ftp.pwd()}"
        )

        candidates = [
            f for f in ftp.nlst()
            if f.startswith(f"rib.{year}{month:02d}{day:02d}") and f.endswith(".bz2")
        ]
        if not candidates:
            raise ValueError(
                f"No RIB archive found for {year}-{month:02d}-{day:02d}"
            )

        # Use the 12:00 file if available, else use the first one
        if any(f.endswith("1200.bz2") for f in candidates):
            target = next(f for f in candidates if f.endswith("1200.bz2"))
        else:
            target = candidates[0]
        self.io_helper.logger.info(
            f"Using target file {target} for download"
        )

        # Download the file
        size = ftp.size(target)
        with open(self.io_helper.raw / target, "wb") as raw_file:
            bytes_done = 0
            chunks_done = 0

            def callback(data: bytes):
                nonlocal bytes_done, chunks_done
                raw_file.write(data)
                bytes_done += len(data)
                chunks_done += 1
                if chunks_done % 100 == 0:
                    percentage = (bytes_done / size) * 100
                    self.io_helper.logger.info(
                        f"Downloaded {percentage:.2f}% of {target}"
                    )

            ftp.retrbinary(f"RETR {target}", callback)
        ftp.close()
        self.io_helper.logger.info(
            f"Downloaded {target} to {self.io_helper.raw}"
        )

        # Process the downloaded file
        processed_path = self.io_helper.processed / f"{target}.processed"
        prefixes = mrtx.parse_mrt_file(
            str(self.io_helper.raw / target)
        )
        mrtx.dump_prefixes_to_file(
            prefixes,
            processed_path,
            source_description=target
        )
        self.io_helper.logger.info(
            f"Processed {target} to {processed_path}"
        )

        return processed_path

    def lookup(
        self,
        addr: IPv4Address | IPv6Address | str,
    ):
        """
        Lookup the ASN for a given IP address.

        Args:
            addr (IPv4Address | IPv6Address | str):
                The IP address to look up.

        Returns:
            asn (int | None):
                The ASN for the given IP address, or None if not found.
        """
        if isinstance(addr, str):
            addr = ip_address(addr)
        if addr.is_private:
            return None
        (asn, _) = self.as_db.lookup(str(addr))
        return asn
