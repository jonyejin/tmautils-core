from cryptography.hazmat.primitives import serialization, hashes
from cryptography.utils import CryptographyDeprecationWarning
import asyncio
import pandas as pd
from dns.rdatatype import RdataType
from ssl import SSLCertVerificationError
import warnings
import multiprocessing as mp
from multiprocessing.synchronize import Event
import threading
import contextlib
import time

from tmautils.common import *
from tmautils.dns import AsyncDnsPythonUtil, dns_msg_semantic_hash

from .cert import get_cert_async

_T = TypeVar("_T")


class DomainProfileMethod(IpcMethodBase, StrEnum):
    GET_CERT = "get_cert"
    GET_DNS = "get_dns"
    GET_CERT_DNS = "get_cert_dns"


class DomainProfileWorker:
    SERVICE = "domain_profile"
    MAX_CONCURRENCY = 64
    GET_TIMEOUT = 0.5

    def __init__(
        self,
        cmd_q: mp.Queue,
        rsp_q: mp.Queue,
        shutdown_event: Event,
        *,
        working_root: str,
        dns_util_kwargs: Optional[dict] = None,
        logging_config: Optional[LogConfig] = None,
    ):
        self.cmd_q = cmd_q
        self.rsp_q = rsp_q
        self.shutdown_event = shutdown_event

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._cmd_thread: Optional[threading.Thread] = None
        self._tasks: Dict[int, asyncio.Task] = {}  # req_id -> task
        self._async_helper = AsyncHelper()

        dns_kwargs = dict(dns_util_kwargs or {})
        dns_working_root = dns_kwargs.pop("working_root", working_root)
        self._dns = AsyncDnsPythonUtil(
            working_root=Path(dns_working_root),
            **dns_kwargs,
        )

        self._log_helper = (
            LogHelper(logging_config) if logging_config else None
        )
        self.logger = get_logger_from_helper(self._log_helper)

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENCY)

        # Start command thread
        self._cmd_thread = threading.Thread(
            target=self._cmd_loop,
            name=f"{self.__class__.__name__}-cmd",
            daemon=True,
        )
        self._cmd_thread.start()

        # announce readiness
        with contextlib.suppress(Exception):
            self.rsp_q.put(IpcMsg.status(IpcStatusCode.READY))

        try:
            self._loop.run_forever()
        finally:
            # Cancel outstanding tasks
            for t in list(self._tasks.values()):
                with contextlib.suppress(Exception):
                    t.cancel()
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(
                    asyncio.gather(
                        *self._tasks.values(), return_exceptions=True
                    )
                )
                self._loop.run_until_complete(
                    self._loop.shutdown_asyncgens()
                )
            self._loop.close()
            with contextlib.suppress(Exception):
                self._async_helper.mpq_put_sync(
                    self.rsp_q, IpcMsg.status(IpcStatusCode.STOPPED), timeout=0.5
                )

    def _cmd_loop(self):
        self.logger.info("Started command loop")

        while True:
            if self.shutdown_event.is_set():
                self.logger.info("Shutdown event set, exiting command loop")
                break

            try:
                msg = self.cmd_q.get()
            except (EOFError, OSError) as exc:
                self.logger.info(
                    f"Command queue closed ({exc}), exiting command loop"
                )
                break
            except Exception:
                continue

            if msg is None:
                self.logger.info("Received sentinel, exiting command loop")
                break

            if (not isinstance(msg, IpcMsg) or
                    msg.service != self.SERVICE or
                    not msg.is_request or
                    not self._loop):
                continue

            self._loop.call_soon_threadsafe(self._accept_request, msg)

        # Stop the event loop
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_loop)

    def _stop_loop(self):
        for t in list(self._tasks.values()):
            with contextlib.suppress(Exception):
                self.logger.info(f"Cancelling task {t.get_name()}")
                t.cancel()
        if self._loop and self._loop.is_running():
            self._loop.stop()
        self.logger.info("Stopped event loop")

    def _accept_request(self, req: IpcMsg):
        if req.req_id in self._tasks and not self._tasks[req.req_id].done():
            # Duplicate req_id
            return

        coro = self._handle_request(req)
        task = self._loop.create_task(
            coro,
            name=f"{self.__class__.__name__}-{req.method}-{req.req_id}"
        )
        self._tasks[req.req_id] = task

        def _done(_):
            self._tasks.pop(req.req_id, None)

        task.add_done_callback(_done)

    async def _handle_request(self, req: IpcMsg):
        assert self._sem is not None
        try:
            async with self._sem:
                method = DomainProfileMethod.get_method(req)
                if method == DomainProfileMethod.GET_CERT:
                    result = await self._get_cert(req.kwargs or {})
                elif method == DomainProfileMethod.GET_DNS:
                    result = await self._get_dns(req.kwargs or {})
                elif method == DomainProfileMethod.GET_CERT_DNS:
                    result = await self._get_cert_dns(req.kwargs or {})
                else:
                    raise ValueError(f"Unknown method {method}")

            await self._async_helper.mpq_put(
                self.rsp_q, req.respond_with(ok=True, result=result)
            )
        except asyncio.CancelledError as e:
            with contextlib.suppress(Exception):
                await self._async_helper.mpq_put(
                    self.rsp_q, req.respond_with(ok=False, error=e)
                )
            raise
        except Exception as e:
            with contextlib.suppress(Exception):
                await self._async_helper.mpq_put(
                    self.rsp_q, req.respond_with(ok=False, error=e)
                )

    async def _get_cert(self, kw: Dict[str, Any]):
        host = kw["host"]
        port = int(kw.get("port", 443))
        sni = kw.get("sni")

        cert_info: dict[str, Any] = {}
        cert_obs: dict[str, Any] = {
            "fingerprint": None,
            "valid": False,
            "error": None,
        }

        cert = None
        try:
            cert = await get_cert_async(
                host, port=port, sni=sni, verify=True
            )
            cert_obs["valid"] = True
        except SSLCertVerificationError as e_verify:
            # Verification failed -> try again without verification
            cert_obs["error"] = repr(e_verify)
            try:
                cert = await get_cert_async(
                    host, port=port, sni=sni, verify=False
                )
            except Exception as e_noverify:
                cert_obs["error"] = repr(e_noverify)
        except Exception as e:
            cert_obs["error"] = repr(e)

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

                cert_obs["fingerprint"] = _safe(
                    lambda: cert.fingerprint(hashes.SHA256()).hex()
                )
                cert_info = {
                    "fingerprint": cert_obs["fingerprint"],
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
                }

        return {
            "timestamp": time.time(),
            "host": host,
            "port": port,
            "sni": sni,
            "cert_info": cert_info,
            "cert_obs": cert_obs,
        }

    async def _get_dns(self, kw: Dict[str, Any]):
        host = kw["host"]

        dns_dict = await self._dns.resolve_rdtypes(
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

        dns_obs: dict[str, Any] = {}
        dns_info: dict[str, bytes] = {}

        for k, v in dns_dict.items():
            if v is None:
                dns_obs[f"{k}_semhash"] = None
                dns_obs[f"{k}_expiry"] = None
                continue

            semhash = dns_msg_semantic_hash(v.response)
            dns_obs[f"{k}_semhash"] = semhash
            dns_obs[f"{k}_expiry"] = v.expiration
            dns_info[semhash] = v.response.to_wire()

        return {
            "timestamp": time.time(),
            "host": host,
            "dns_obs": dns_obs,
            "dns_info": dns_info,
        }

    async def _get_cert_dns(self, kw: Dict[str, Any]):
        cert_res, dns_res = await asyncio.gather(self._get_cert(kw), self._get_dns(kw))
        return cert_res, dns_res


class DomainProfileUtil:
    """
    Utility to grab and store TLS certificate and DNS information for domains.
    Stores data in a SQLite database with two tables: `cert_store` and `dns_store`.
    For clean shutdown, call `aclose()` (async) or `close()` (sync).

    Args:
        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        num_workers (int):
            Number of worker processes to spawn for handling requests.
            Default is 1.

        dns_util_kwargs (dict | None):
            Additional keyword arguments to pass to the AsyncDnsPythonUtil instances
            used by each worker process.
            See the AsyncDnsPythonUtil documentation for more details.
            Note: Unless `working_root` needs to be different for the DNS utility,
            there is no need to pass it here (it will be set to the same value as the
            `working_root` parameter of this class).

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
    GET_TIMEOUT = 0.5
    ALL_STOPPED_TIMEOUT = 10.0
    JOIN_TIMEOUT = 3.0

    def __init__(
        self,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        num_workers: int = 1,
        dns_util_kwargs: dict | None = None,
        **kwargs
    ):
        # Initialize IoHelper
        working_root = IOHelper.handle_working_root_data_dir(
            working_root, data_dir
        )
        self.io_helper = IOHelper(
            self.__class__.__name__,
            working_root=working_root,
            **kwargs,
        )
        self.db_path = self.io_helper.raw / "domain.sqlite"

        self.db = SqliteDatabase(
            self.db_path,
            log_helper=self.io_helper.log_helper,
            offload_to_worker=True,
            write_buffering=True,
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

        # Worker processes
        self._ctx = mp.get_context("spawn")
        self._cmd_q = self._ctx.Queue()  # Shared command queue
        self._rsp_q = self._ctx.Queue()  # Shared response queue
        self._workers: list[mp.Process] = []
        self._shutdown_event = self._ctx.Event()
        self._stopped_lock = threading.Lock()
        self._stopped_left = 0  # Workers left to stop
        self._all_stopped = threading.Event()
        self._closed = False

        # IPC
        self._req_id = 0
        self._req_lock = threading.Lock()
        self._waiters: Dict[int,
                            tuple[asyncio.AbstractEventLoop,
                                  asyncio.Future,
                                  DomainProfileMethod]] = {}
        self._waiters_lock = threading.Lock()
        self._async_helper = AsyncHelper()

        # Start workers
        for i in range(max(1, int(num_workers))):
            p = self._ctx.Process(
                target=self._child_entry,
                args=(
                    self._cmd_q,
                    self._rsp_q,
                    self._shutdown_event,
                    str(self.io_helper.working_root),
                    dns_util_kwargs or {},
                    self.io_helper.get_worker_logging_config(),
                ),
                daemon=False,
                name=f"{self.__class__.__name__}-worker-{i}"
            )
            p.start()
            self._workers.append(p)
        with self._stopped_lock:
            self._stopped_left = len(self._workers)

        # Start response reader
        self._resp_reader_thread = threading.Thread(
            target=self._resp_read_loop,
            name=f"{self.__class__.__name__}-reader",
            daemon=True,
        )
        self._resp_reader_thread.start()

        self.io_helper.logger.info(
            f"{self.__class__.__name__} initialized with DB at {self.db_path} "
            f"and {num_workers} worker(s)."
        )

    @staticmethod
    def _child_entry(
        cmd_q: mp.Queue,
        rsp_q: mp.Queue,
        shutdown_event: Event,
        working_root: str,
        dns_util_kwargs: Optional[dict] = None,
        logging_config: Optional[LogConfig] = None,
    ):
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        worker = DomainProfileWorker(
            cmd_q, rsp_q, shutdown_event,
            working_root=working_root,
            dns_util_kwargs=dns_util_kwargs,
            logging_config=logging_config,
        )
        worker.run()

    def _next_id(self):
        with self._req_lock:
            rid = self._req_id
            self._req_id += 1
            return rid

    def _resp_read_loop(self):
        while not self._closed:
            try:
                msg = self._rsp_q.get(timeout=self.GET_TIMEOUT)
            except Exception:
                continue

            if not isinstance(msg, IpcMsg):
                continue

            if msg.is_status:
                self.io_helper.logger.info(
                    f"Received status from worker PID {msg.sender_pid}: {msg.status_code}"
                )
                if msg.status_code == IpcStatusCode.STOPPED:
                    # Keep track of how many workers left to stop
                    with self._stopped_lock:
                        self._stopped_left = max(0, self._stopped_left - 1)
                        self.io_helper.logger.info(
                            f"Workers left to stop: {self._stopped_left}"
                        )
                        if self._stopped_left == 0:
                            self._all_stopped.set()
                continue

            if not msg.is_response or msg.req_id is None:
                continue

            with self._waiters_lock:
                item = self._waiters.pop(msg.req_id, None)

            if item is None:
                # No waiter => nothing to do
                continue

            loop, fut, method = item

            # Handle exceptions
            if not msg.ok:
                try:
                    raise_from_payload(msg.error)
                except Exception as e:
                    exc = e
                loop.call_soon_threadsafe(fut.set_exception, exc)
                continue

            # Handle result
            try:
                loop.call_soon_threadsafe(fut.set_result, msg.result)
            except Exception as e:
                loop.call_soon_threadsafe(fut.set_exception, e)

    async def _rpc(self, method: DomainProfileMethod, **kwargs):
        if self._closed:
            raise RuntimeError(
                "DomainProfileUtil is closed, cannot make new requests"
            )

        loop = asyncio.get_running_loop()
        req_id = self._next_id()
        fut: asyncio.Future = loop.create_future()

        with self._waiters_lock:
            self._waiters[req_id] = (loop, fut, method)

        req = IpcMsg.request(
            req_id=req_id,
            service=DomainProfileWorker.SERVICE,
            method=method,
            **kwargs,
        )
        await self._async_helper.mpq_put(self._cmd_q, req)

        return await fut

    def _handle_cert_result(self, result: dict):
        cert_info = result.get("cert_info") or {}
        fingerprint = cert_info.get("fingerprint")
        if fingerprint:
            cert_info_df = pd.DataFrame.from_records(
                [cert_info],
                columns=self.CERT_INFO["schema"].keys(),
            )
            self.cert_info_table.insert_df(cert_info_df, if_exists="ignore")
        else:
            cert_info_df = pd.DataFrame(
                columns=self.CERT_INFO["schema"].keys()
            )

        cert_obs = {
            "timestamp": float(result.get("timestamp", time.time())),
            "host": result.get("host"),
            "port": int(result.get("port", 443)),
            "sni": result.get("sni"),
            "fingerprint": fingerprint,
            "valid": bool((result.get("cert_obs") or {}).get("valid", False)),
            "error": (result.get("cert_obs") or {}).get("error"),
        }
        cert_obs_df = pd.DataFrame.from_records(
            [cert_obs],
            columns=self.CERT_OBSERVATION["schema"].keys(),
        )
        self.cert_obs_table.insert_df(cert_obs_df)

        return self._cert_join(cert_obs_df, cert_info_df)

    def _cert_join(self, obs_df: pd.DataFrame, info_df: pd.DataFrame):
        return obs_df.merge(
            info_df,
            on="fingerprint",
            how="left",
        )

    def _handle_dns_result(self, result: dict):
        dns_info = result.get("dns_info") or {}
        if dns_info:
            dns_info_df = pd.DataFrame.from_records(
                [{"hash": h, "wire_bytes": b} for h, b in dns_info.items()],
                columns=self.DNS_INFO["schema"].keys(),
            )
            self.dns_info_table.insert_df(dns_info_df, if_exists="ignore")
        else:
            dns_info_df = pd.DataFrame(columns=self.DNS_INFO["schema"].keys())

        dns_obs = result.get("dns_obs") or {}
        obs_row = {
            "timestamp": float(result.get("timestamp", time.time())),
            "host": result.get("host"),
        }
        for t in self.DNS_RECORD_TYPES:
            obs_row[f"{t}_semhash"] = dns_obs.get(f"{t}_semhash")
            obs_row[f"{t}_expiry"] = dns_obs.get(f"{t}_expiry")

        dns_obs_df = pd.DataFrame.from_records(
            [obs_row],
            columns=self.DNS_OBSERVATION["schema"].keys()
        )
        self.dns_obs_table.insert_df(dns_obs_df)

        return self._dns_join(dns_obs_df, dns_info_df)

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

    def _handle_cert_dns_result(self, result: tuple[dict, dict]):
        cert_result, dns_result = result
        return self._handle_cert_result(cert_result), self._handle_dns_result(dns_result)

    async def grab_cert_async(
        self,
        host: str,
        port: int = 443,
        sni: Optional[str] = None,
    ) -> pd.DataFrame:
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
        result = await self._rpc(
            DomainProfileMethod.GET_CERT,
            host=host,
            port=port,
            sni=sni,
        )
        return await asyncio.to_thread(self._handle_cert_result, result)

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

    async def grab_dns_async(self, host: str) -> pd.DataFrame:
        """
        Grab DNS records for a host, store them in the database, and return the stored row.
        This is an async function. For a sync version, use `grab_dns()`.

        Args:
            host (str):
                The hostname to resolve.

        Returns:
            A single-row DataFrame with the stored DNS information.
        """

        result = await self._rpc(
            DomainProfileMethod.GET_DNS,
            host=host,
        )
        return await asyncio.to_thread(self._handle_dns_result, result)

    def grab_dns(self, host: str):
        """
        Synchronous wrapper for `grab_dns_async()`.
        """

        return run_coro_sync(self.grab_dns_async(host))

    async def grab_cert_dns_async(
        self,
        host: str,
        port: int = 443,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
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

        result = await self._rpc(
            DomainProfileMethod.GET_CERT_DNS,
            host=host,
            port=port,
        )
        return await asyncio.to_thread(self._handle_cert_dns_result, result)

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

    def get_all_certs(self):
        obs_df = self.cert_obs_table.query_all()
        info_df = self.cert_info_table.query_all()
        return self._cert_join(obs_df, info_df)

    def get_all_dns(self):
        obs_df = self.dns_obs_table.query_all()
        info_df = self.dns_info_table.query_all()
        return self._dns_join(obs_df, info_df)

    async def aclose(self):
        if self._closed:
            return

        # Signal shutdown to workers
        self.io_helper.logger.info("Shutting down workers...")
        with contextlib.suppress(Exception):
            self._shutdown_event.set()
            if self._cmd_q is not None:
                # Send sentinel to all workers
                for _ in self._workers:
                    self._async_helper.mpq_put_sync(
                        self._cmd_q, None, timeout=0.5
                    )
                # Close command queue
                self._cmd_q.close()
                self._cmd_q.cancel_join_thread()
                self._cmd_q = None

        # Cancel any outstanding waiters
        with self._waiters_lock:
            items = list(self._waiters.items())
            self._waiters.clear()
        for _, (loop, fut, _) in items:
            if not fut.done():
                loop.call_soon_threadsafe(
                    fut.set_exception,
                    RuntimeError(
                        "DomainProfileUtil closed before resolving future"
                    ),
                )

        # Wait for workers to acknowledge STOPPED
        if not self._all_stopped.wait(timeout=self.ALL_STOPPED_TIMEOUT):
            self.io_helper.logger.warning(
                "Timed out waiting for all workers to acknowledge STOPPED."
            )

        # Join workers
        for p in self._workers:
            p.join(timeout=self.JOIN_TIMEOUT)
            if p.is_alive():
                self.io_helper.logger.warning(
                    f"Worker PID {p.pid} did not exit in time, terminating."
                )
                with contextlib.suppress(Exception):
                    p.terminate()
        self.io_helper.logger.info("All workers stopped.")

        # Join reader
        self._closed = True
        if self._resp_reader_thread and self._resp_reader_thread.is_alive():
            self._resp_reader_thread.join(timeout=self.JOIN_TIMEOUT)

        # Close DB
        with contextlib.suppress(Exception):
            self.db.close()

    def close(self):
        return run_coro_sync(self.aclose())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
