from cryptography.hazmat.primitives import serialization, hashes
import asyncio
import pandas as pd
from dns.rdatatype import RdataType
from concurrent.futures import ThreadPoolExecutor
from ssl import SSLCertVerificationError

from tmautils.common import *
from tmautils.dns import AsyncDnsPythonUtil

from .cert import get_cert_async

_T = TypeVar("_T")


class DomainProfileUtil:
    """
    Utility to grab and store TLS certificate and DNS information for domains.
    Stores data in a SQLite database with two tables: `cert_store` and `dns_store`.
    Note: This class runs a dedicated single-threaded executor for database operations.
    This means that you MUST go through the provided `query..` methods to access the database,
    and not access via the `db` or `cert_table`/`dns_table` attributes directly.

    Args:
        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        **kwargs:
            Additional keyword arguments for IOHelper and AsyncDnsPythonUtil.
            See their documentation for more details.
    """

    CERT_SCHEMA = {
        "timestamp": float,
        "host": str,
        "raw_cert": bytes,
        "version": int,
        "subject": str,
        "issuer": str,
        "serial_number": str,
        "not_valid_before": float,
        "not_valid_after": float,
        "signature_algorithm": str,
        "hash_algorithm": str,
        "fingerprint": str,
        "valid": bool,
        "error": str,
    }
    DNS_SCHEMA = {
        "timestamp": float,
        "host": str,
        "A": str,
        "AAAA": str,
        "CNAME": str,
        "CAA": str,
        "DMARC": str,
        "DNSKEY": str,
        "DS": str,
        "MX": str,
        "NS": str,
        "SOA": str,
        "SPF": str,
        "TXT": str,
    }

    def __init__(
        self,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        **kwargs
    ):
        from logging import ERROR

        # Initialize IoHelper and DNS util
        working_root = IOHelper.handle_working_root_data_dir(
            working_root, data_dir
        )
        self.io_helper = IOHelper(
            self.__class__.__name__,
            working_root=working_root,
            **kwargs,
        )
        self.dns_util = AsyncDnsPythonUtil(
            working_root=working_root,
            log_level_console=ERROR,
            **kwargs,
        )
        self.db_path = self.io_helper.raw / "domain.sqlite"

        # Dedicated DB executor (single thread)
        self._db_exec = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{self.__class__.__name__}-dbexec"
        )

        # Build database and tables inside the db executor thread
        def _init_db():
            db = SqliteDatabase(self.db_path, logger=self.io_helper.logger)
            cert_table = db.register_table(
                "cert_store",
                self.CERT_SCHEMA,
                table_constraints=[
                    "PRIMARY KEY (timestamp, host)",
                ],
                indices=[
                    ["host"], ["subject"], ["issuer"], ["not_valid_after"],
                ],
            )
            dns_table = db.register_table(
                "dns_store",
                self.DNS_SCHEMA,
                table_constraints=[
                    "PRIMARY KEY (timestamp, host)",
                ],
            )
            return db, cert_table, dns_table
        self.db, self.cert_table, self.dns_table = self._db_exec.submit(
            _init_db
        ).result()
        self._closed = False

    async def _db_call(self, fn: Callable[..., _T], *args, **kwargs) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._db_exec, lambda: fn(*args, **kwargs))

    async def grab_cert_async(
        self,
        host: str,
        port: int = 443,
        sni: Optional[str] = None,
    ):
        """
        Grab the TLS certificate from a host:port, store it in the database, and return the stored row.
        For a sync version, use `grab_cert()`.

        Args:
            host (str):
                The hostname or IP address to connect to.

            port (int):
                The TCP port to connect to.
                Default is 443.

            sni (str | None):
                Optional SNI hostname to use in the TLS handshake.
                Useful when passing an IP address as `host`.
                If None, the `host` value will be used.
                Default is None.

        Returns:
            A single-row DataFrame with the obtained certificate information.
        """

        cert = None
        valid = False
        err = None
        try:
            # First, try with verification
            cert = await get_cert_async(host, port=port, sni=sni, verify=True)
            valid = True
        except SSLCertVerificationError as e_verify:
            # Verification failed -> try again without verification
            self.io_helper.logger.info(
                f"Cert verification failed for {host}:{port}: {e_verify}. "
                f"Retrying without verification."
            )
            err = repr(e_verify)
            try:
                cert = await get_cert_async(host, port=port, sni=sni, verify=False)
            except Exception as e_noverify:
                self.io_helper.logger.info(
                    f"Failed to get cert from {host}:{port} (without verification): {e_noverify}"
                )
                err = repr(e_noverify)
        except Exception as e:
            self.io_helper.logger.info(
                f"Failed to get cert from {host}:{port}: {e}"
            )
            err = repr(e)

        timestamp = pd.Timestamp.now(tz="UTC").timestamp()
        if cert is None:
            cert_df = pd.DataFrame.from_records([{
                "timestamp": timestamp,
                "host": host,
                "error": err,
            }])
        else:
            cert_df = pd.DataFrame.from_records([{
                "timestamp": timestamp,
                "host": host,
                "raw_cert": cert.public_bytes(encoding=serialization.Encoding.DER),
                "version": cert.version.value,
                "subject": cert.subject.rfc4514_string(),
                "issuer": cert.issuer.rfc4514_string(),
                "serial_number": str(cert.serial_number),
                "not_valid_before": pd.to_datetime(cert.not_valid_before_utc, utc=True).timestamp(),
                "not_valid_after": pd.to_datetime(cert.not_valid_after_utc, utc=True).timestamp(),
                "signature_algorithm": cert.signature_algorithm_oid.dotted_string,
                "hash_algorithm": getattr(cert.signature_hash_algorithm, "name", None),
                "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
                "valid": valid,
                "error": err,
            }])

        await self._db_call(self.cert_table.insert_df, cert_df)
        return cert_df

    def grab_cert(
        self,
        host: str,
        port: int = 443,
        sni: Optional[str] = None,
    ):
        """
        Synchronous wrapper for `grab_cert_async()`.
        """

        return run_coro_sync(
            self.grab_cert_async(host, port, sni=sni)
        )

    async def grab_dns_async(self, host: str):
        """
        Grab DNS records for a host, store them in the database, and return the stored row.
        This is an async function. For a sync version, use `grab_dns()`.

        Args:
            host (str):
                The hostname to resolve.

        Returns:
            A single-row DataFrame with the stored DNS information.
        """

        # Grab DNS records and convert responses to text
        dns_dict = await self.dns_util.resolve_rdtypes(
            host,
            a=True, aaaa=True, caa=True,
            cname=True, dmarc=True, dnskey=True,
            ds=True, mx=True, ns=True,
            soa=True, spf=True, txt=True,
        )

        # Handle special rdtypes and convert responses to text
        spf_dmarc_map = {"_spf": "SPF", "_dmarc": "DMARC"}
        dns_dict = {
            (k.name if isinstance(k, RdataType) else spf_dmarc_map[k]):
            (v.response.to_text() if v is not None else None)
            for k, v in dns_dict.items()
        }

        # Convert to dataframe
        timestamp = pd.Timestamp.now(tz="UTC").timestamp()
        dns_df = pd.DataFrame.from_records([{
            "timestamp": timestamp,
            "host": host,
            **dns_dict,
        }])

        await self._db_call(self.dns_table.insert_df, dns_df)
        return dns_df

    def grab_dns(self, host: str):
        """
        Synchronous wrapper for `grab_dns_async()`.
        """

        return run_coro_sync(self.grab_dns_async(host))

    async def grab_cert_dns_async(
        self,
        host: str,
        port: int = 443,
    ):
        """
        Grab both the TLS certificate and DNS records for a host, store them in the database,
        and return them.
        This is an async function. For a sync version, use `grab_cert_dns()`.

        Args:
            host (str):
                The hostname to connect to and resolve.

            port (int):
                The TCP port to connect to for the TLS certificate.
                Default is 443.

        Returns:
            A tuple of two elements.
            The first element is either a single-row DataFrame with the stored certificate information.
            The second element is a single-row DataFrame with the stored DNS information.
        """

        return await asyncio.gather(
            self.grab_cert_async(host, port=port),
            self.grab_dns_async(host),
        )

    def grab_cert_dns(
        self,
        host: str,
        port: int = 443,
    ):
        """
        Synchronous wrapper for `grab_cert_dns_async()`.
        """

        return run_coro_sync(
            self.grab_cert_dns_async(host, port)
        )

    def query(self, table: SqliteTable, sql: str, params: Tuple = ()):
        """
        Execute a SQL query on the specified table and return the results as a DataFrame.

        Args:
            table (SqliteTable):
                The table to query.

            sql (str):
                The SQL query to execute.

            params (Tuple):
                Optional parameters for the SQL query.

        Returns:
            A DataFrame with the query results.
        """
        return run_coro_sync(self._db_call(table.query, sql, params))

    def query_dns(self, sql: str, params: Tuple = ()):
        """
        Query the DNS table with a SQL query and return the results as a DataFrame.
        """
        return self.query(self.dns_table, sql, params)

    def query_cert(self, sql: str, params: Tuple = ()):
        """
        Query the certificate table with a SQL query and return the results as a DataFrame.
        """
        return self.query(self.cert_table, sql, params)

    def query_all(self, table: SqliteTable):
        """
        Query all rows from the specified table and return them as a DataFrame.

        Args:
            table (SqliteTable):
                The table to query.

        Returns:
            A DataFrame with all rows from the table.
        """
        return run_coro_sync(self._db_call(table.query_all))

    def query_all_dns(self):
        """
        Return all rows from the DNS table as a DataFrame.
        """
        return self.query_all(self.dns_table)

    def query_all_cert(self):
        """
        Return all rows from the certificate table as a DataFrame.
        """
        return self.query_all(self.cert_table)

    async def aclose(self):
        if self._closed:
            return

        def _close_db():
            try:
                self.db.close()
            except Exception as e:
                self.io_helper.logger.warning(f"Error closing DB: {e}")
        await self._db_call(_close_db)
        self._db_exec.shutdown(wait=True)
        self._closed = True

    def close(self):
        return run_coro_sync(self.aclose())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
