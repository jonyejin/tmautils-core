from cryptography.hazmat.primitives import serialization, hashes
from cryptography.utils import CryptographyDeprecationWarning
import asyncio
import pandas as pd
from dns.rdatatype import RdataType
from concurrent.futures import ThreadPoolExecutor
from ssl import SSLCertVerificationError
import warnings

from tmautils.common import *
from tmautils.dns import AsyncDnsPythonUtil, dns_msg_semantic_hash

from .cert import get_cert_async

_T = TypeVar("_T")


class DomainProfileUtil:
    """
    Utility to grab and store TLS certificate and DNS information for domains.
    Stores data in a SQLite database with two tables: `cert_store` and `dns_store`.
    Note: This class runs a dedicated single-threaded executor for database operations.
    This means that you MUST go through the provided `query..` methods to access the database.

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

    CERT_OBSERVATION = {
        "schema": {
            "timestamp": float,
            "host": str,
            "port": int,
            "sni": str,
            "fingerprint": str,
            "valid": bool,
            "error": str,
        },
        "constraints": [
            "PRIMARY KEY (timestamp, host, port, sni)",
            "FOREIGN KEY (fingerprint) REFERENCES cert_info(fingerprint)",
        ],
        "indices": [
            ["host"], ["fingerprint"], ["timestamp"], ["sni"],
        ],
    }
    CERT_INFO = {
        "schema": {
            "fingerprint": str,
            "raw_cert": bytes,
            "version": int,
            "subject": str,
            "issuer": str,
            "serial_number": str,
            "not_valid_before": float,
            "not_valid_after": float,
            "signature_algorithm": str,
            "hash_algorithm": str,
        },
        "constraints": [
            "PRIMARY KEY (fingerprint)",
        ],
        "indices": [
            ["subject"], ["issuer"], ["not_valid_after"],
        ],
    }
    DNS_OBSERVATION = {
        "schema": {
            "timestamp": float,
            "host": str,
            "A_semhash": str, "A_expiry": float,
            "AAAA_semhash": str, "AAAA_expiry": float,
            "CNAME_semhash": str, "CNAME_expiry": float,
            "CAA_semhash": str, "CAA_expiry": float,
            "DMARC_semhash": str, "DMARC_expiry": float,
            "DNSKEY_semhash": str, "DNSKEY_expiry": float,
            "DS_semhash": str, "DS_expiry": float,
            "MX_semhash": str, "MX_expiry": float,
            "NS_semhash": str, "NS_expiry": float,
            "SOA_semhash": str, "SOA_expiry": float,
            "SPF_semhash": str, "SPF_expiry": float,
            "TXT_semhash": str, "TXT_expiry": float,
        },
        "constraints": [
            "PRIMARY KEY (timestamp, host)",
            "FOREIGN KEY (A_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (AAAA_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (CNAME_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (CAA_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (DMARC_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (DNSKEY_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (DS_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (MX_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (NS_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (SOA_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (SPF_semhash) REFERENCES dns_info(hash)",
            "FOREIGN KEY (TXT_semhash) REFERENCES dns_info(hash)",
        ],
        "indices": [
            ["host"], ["timestamp"],
        ],
    }
    DNS_INFO = {
        "schema": {
            "hash": str,
            "wire_bytes": bytes,
        },
        "constraints": [
            "PRIMARY KEY (hash)",
        ],
        "indices": [
            ["hash"],
        ],
    }
    DNS_RECORD_TYPES = [
        t[:-8] for t in DNS_OBSERVATION["schema"].keys() if t.endswith("_semhash")
    ]

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

        self.db = SqliteDatabase(
            self.db_path,
            logger=self.io_helper.logger,
            offload_to_worker=True,
        )
        self.cert_info_table = self.db.register_table(
            "cert_info",
            schema=self.CERT_INFO["schema"],
            table_constraints=self.CERT_INFO["constraints"],
            indices=self.CERT_INFO["indices"],
        )
        self.cert_obs_table = self.db.register_table(
            "cert_observation",
            schema=self.CERT_OBSERVATION["schema"],
            table_constraints=self.CERT_OBSERVATION["constraints"],
            indices=self.CERT_OBSERVATION["indices"],
        )
        self.dns_info_table = self.db.register_table(
            "dns_info",
            schema=self.DNS_INFO["schema"],
            table_constraints=self.DNS_INFO["constraints"],
            indices=self.DNS_INFO["indices"],
        )
        self.dns_obs_table = self.db.register_table(
            "dns_observation",
            schema=self.DNS_OBSERVATION["schema"],
            table_constraints=self.DNS_OBSERVATION["constraints"],
            indices=self.DNS_OBSERVATION["indices"],
        )

        self.io_helper.logger.info(
            f"{self.__class__.__name__} initialized with DB at {self.db_path}"
        )

    def _cert_join(self, obs_df: pd.DataFrame, info_df: pd.DataFrame):
        return obs_df.merge(
            info_df,
            on="fingerprint",
            how="left",
        )

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

        # Prepare and insert cert info row if we got a cert
        fingerprint = None
        cert_info_df = pd.DataFrame(columns=self.CERT_INFO["schema"].keys())
        if cert is not None:
            with warnings.catch_warnings():
                # Ignore cryptography deprecation warnings
                # We cannot do anything about the certs we receive
                warnings.filterwarnings(
                    "ignore",
                    category=CryptographyDeprecationWarning
                )
                warnings.filterwarnings("ignore", category=DeprecationWarning)
                warnings.filterwarnings("ignore", category=UserWarning)

                def _safe(getter, default=None):
                    try:
                        return getter()
                    except Exception:
                        return default

                fingerprint = _safe(
                    lambda: cert.fingerprint(hashes.SHA256()).hex()
                )
                cert_info_df = pd.DataFrame.from_records([{
                    "fingerprint": fingerprint,
                    "raw_cert": _safe(
                        lambda: cert.public_bytes(
                            serialization.Encoding.DER
                        ),
                    ),
                    "version": _safe(lambda: cert.version.value),
                    "subject": _safe(lambda: cert.subject.rfc4514_string()),
                    "issuer": _safe(lambda: cert.issuer.rfc4514_string()),
                    "serial_number": _safe(lambda: str(cert.serial_number)),
                    "not_valid_before": _safe(lambda: cert.not_valid_before_utc.timestamp()),
                    "not_valid_after": _safe(lambda: cert.not_valid_after_utc.timestamp()),
                    "signature_algorithm": _safe(
                        lambda: cert.signature_algorithm_oid.dotted_string
                    ),
                    "hash_algorithm": _safe(lambda: cert.signature_hash_algorithm.name),
                }])

            self.cert_info_table.insert_df(cert_info_df, if_exists="ignore")

        # Prepare and insert observation row
        cert_obs_df = pd.DataFrame.from_records([{
            "timestamp": pd.Timestamp.now(tz="UTC").timestamp(),
            "host": host,
            "port": port,
            "sni": sni,
            "fingerprint": fingerprint,
            "valid": valid,
            "error": err,
        }])
        self.cert_obs_table.insert_df(cert_obs_df)

        # Join cert_obs_df with cert_info_df on fingerprint to return full info
        return self._cert_join(cert_obs_df, cert_info_df)

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

    def _dns_join(self, obs_df: pd.DataFrame, info_df: pd.DataFrame):
        for col in self.DNS_RECORD_TYPES:
            obs_df[col] = pd.Series([None] * len(obs_df), dtype="object")

        if info_df.empty:
            return obs_df

        wire_bytes_map = info_df.set_index("hash")["wire_bytes"]

        # Map all _semhash columns to wire_bytes
        for col in [c for c in obs_df.columns if c.endswith("_semhash")]:
            obs_df[col.replace("_semhash", "")] = obs_df[col].map(
                wire_bytes_map
            )

        # Drop all _semhash columns
        obs_df = obs_df.drop(
            columns=[c for c in obs_df.columns if c.endswith("_semhash")])

        return obs_df

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

        # Handle special rdtypes
        spf_dmarc_map = {"_spf": "SPF", "_dmarc": "DMARC"}
        dns_dict = {
            (k.name if isinstance(k, RdataType) else spf_dmarc_map[k]): v
            for k, v in dns_dict.items()
        }

        dns_obs_dict = {
            "timestamp": pd.Timestamp.now(tz="UTC").timestamp(),
            "host": host,
        }
        dns_info_rows = []

        for k, v in dns_dict.items():
            if v is None:
                dns_obs_dict[f"{k}_semhash"] = None
                dns_obs_dict[f"{k}_expiry"] = None
                continue

            dns_obs_dict[f"{k}_semhash"] = dns_msg_semantic_hash(v.response)
            dns_obs_dict[f"{k}_expiry"] = v.expiration

            dns_info_rows.append({
                "hash": dns_obs_dict[f"{k}_semhash"],
                "wire_bytes": v.response.to_wire(),
            })

        # Insert DNS info rows
        dns_info_df = pd.DataFrame.from_records(dns_info_rows)
        if not dns_info_df.empty:
            self.dns_info_table.insert_df(dns_info_df, if_exists="ignore")

        # Insert DNS observation row
        dns_obs_df = pd.DataFrame.from_records([dns_obs_dict])
        self.dns_obs_table.insert_df(dns_obs_df)

        return self._dns_join(dns_obs_df, dns_info_df)

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

    async def aclose(self):
        if not self.db:
            return

        try:
            self.db.close()
            self.db = None
        except Exception as e:
            self.io_helper.logger.warning(f"Error closing DB: {e}")

    def close(self):
        return run_coro_sync(self.aclose())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
