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
import pyarrow as pa
from pydantic import BaseModel

from tmautils.common import *
from tmautils.dns import AsyncDnsPythonUtil, dns_msg_semantic_hash

from .cert import get_cert_async


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

        log_helper = LogHelper(logging_config) if logging_config else None
        self.logger = get_logger_from_helper(log_helper)

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
            "effective_host": sni if sni else host,
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


class CertInfoModel(BaseModel):
    fingerprint: str
    raw_cert: Optional[bytes] = None
    version: Optional[int] = None
    subject: Optional[str] = None
    issuer: Optional[str] = None
    serial_number: Optional[str] = None
    not_valid_before: Optional[float] = None
    not_valid_after: Optional[float] = None
    signature_algorithm: Optional[str] = None
    hash_algorithm: Optional[str] = None

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("fingerprint", pa.string()),
        pa.field("raw_cert", pa.binary()),
        pa.field("version", pa.int64()),
        pa.field("subject", pa.string()),
        pa.field("issuer", pa.string()),
        pa.field("serial_number", pa.string()),
        pa.field("not_valid_before", pa.timestamp("us")),
        pa.field("not_valid_after", pa.timestamp("us")),
        pa.field("signature_algorithm", pa.string()),
        pa.field("hash_algorithm", pa.string()),
    ])


class CertObservationModel(BaseModel):
    timestamp: float
    host: str
    port: int
    sni: Optional[str] = None
    effective_host: str
    fingerprint: Optional[str] = None
    valid: bool = False
    error: Optional[str] = None

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("timestamp", pa.timestamp("us")),
        pa.field("host", pa.string()),
        pa.field("port", pa.int64()),
        pa.field("sni", pa.string()),
        pa.field("effective_host", pa.string()),
        pa.field("fingerprint", pa.string()),
        pa.field("valid", pa.bool_()),
        pa.field("error", pa.string()),
    ])


class DnsInfoModel(BaseModel):
    hash: str
    wire_bytes: bytes

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("hash", pa.string()),
        pa.field("wire_bytes", pa.binary()),
    ])


class DnsObservationModel(BaseModel):
    timestamp: float
    host: str
    A_semhash: Optional[str] = None
    A_expiry: Optional[float] = None
    AAAA_semhash: Optional[str] = None
    AAAA_expiry: Optional[float] = None
    CAA_semhash: Optional[str] = None
    CAA_expiry: Optional[float] = None
    CNAME_semhash: Optional[str] = None
    CNAME_expiry: Optional[float] = None
    DMARC_semhash: Optional[str] = None
    DMARC_expiry: Optional[float] = None
    DNSKEY_semhash: Optional[str] = None
    DNSKEY_expiry: Optional[float] = None
    DS_semhash: Optional[str] = None
    DS_expiry: Optional[float] = None
    MX_semhash: Optional[str] = None
    MX_expiry: Optional[float] = None
    NS_semhash: Optional[str] = None
    NS_expiry: Optional[float] = None
    SOA_semhash: Optional[str] = None
    SOA_expiry: Optional[float] = None
    SPF_semhash: Optional[str] = None
    SPF_expiry: Optional[float] = None
    TXT_semhash: Optional[str] = None
    TXT_expiry: Optional[float] = None

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("timestamp", pa.timestamp("us")),
        pa.field("host", pa.string()),
        pa.field("A_semhash", pa.string()),
        pa.field("A_expiry", pa.timestamp("us")),
        pa.field("AAAA_semhash", pa.string()),
        pa.field("AAAA_expiry", pa.timestamp("us")),
        pa.field("CAA_semhash", pa.string()),
        pa.field("CAA_expiry", pa.timestamp("us")),
        pa.field("CNAME_semhash", pa.string()),
        pa.field("CNAME_expiry", pa.timestamp("us")),
        pa.field("DMARC_semhash", pa.string()),
        pa.field("DMARC_expiry", pa.timestamp("us")),
        pa.field("DNSKEY_semhash", pa.string()),
        pa.field("DNSKEY_expiry", pa.timestamp("us")),
        pa.field("DS_semhash", pa.string()),
        pa.field("DS_expiry", pa.timestamp("us")),
        pa.field("MX_semhash", pa.string()),
        pa.field("MX_expiry", pa.timestamp("us")),
        pa.field("NS_semhash", pa.string()),
        pa.field("NS_expiry", pa.timestamp("us")),
        pa.field("SOA_semhash", pa.string()),
        pa.field("SOA_expiry", pa.timestamp("us")),
        pa.field("SPF_semhash", pa.string()),
        pa.field("SPF_expiry", pa.timestamp("us")),
        pa.field("TXT_semhash", pa.string()),
        pa.field("TXT_expiry", pa.timestamp("us")),
    ])


class DomainProfileUtil:
    """
    Utility to grab and store TLS certificate and DNS information for domains.
    Backed by DuckLake tables/views defined in `domain_profile.sql`.
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

        row_thresh (int):
            Row threshold for DuckLakeBufferedWriter batching.
            Default is 10,000.

        time_thresh_sec (float):
            Time threshold (in seconds) for DuckLakeBufferedWriter batching.
            Default is 240.0 seconds.

        jitter (float):
            Jitter factor for DuckLakeBufferedWriter batching.
            Default is 0.2.

        **kwargs:
            Additional keyword arguments for IOHelper and AsyncDnsPythonUtil.
            See their documentation for more details.
    """

    GET_TIMEOUT = 0.5
    ALL_STOPPED_TIMEOUT = 10.0
    JOIN_TIMEOUT = 3.0

    # Default batching thresholds
    DEFAULT_ROW_THRESH = 10000
    DEFAULT_TIME_THRESH_SEC = 240.0
    DEFAULT_JITTER = 0.2

    DNS_RECORD_TYPES = [
        "A", "AAAA", "CAA", "CNAME", "DMARC", "DNSKEY",
        "DS", "MX", "NS", "SOA", "SPF", "TXT",
    ]

    def __init__(
        self,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        num_workers: int = 1,
        dns_util_kwargs: dict | None = None,
        *,
        row_thresh: int = DEFAULT_ROW_THRESH,
        time_thresh_sec: float = DEFAULT_TIME_THRESH_SEC,
        jitter: float = DEFAULT_JITTER,
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

        # DuckLake paths + schema
        catalog_path = (self.io_helper.raw / "catalog.sqlite").resolve()
        data_path = (self.io_helper.raw / "data").resolve()
        data_path.mkdir(parents=True, exist_ok=True)
        schema_sql_path = Path(__file__).with_name("domain_profile.sql")
        schema_sql = Path(schema_sql_path).read_text(encoding="utf-8")

        # DuckLake connection
        self._lake = DuckLakeStore(log_helper=self.io_helper.log_helper)
        self._lake.attach_lake(
            alias="lake",
            catalog_path=f"sqlite:{catalog_path}",
            data_path=str(data_path),
            options=["META_JOURNAL_MODE 'WAL'", "META_BUSY_TIMEOUT 500"],
            extensions=("sqlite",),
            schema_sql=schema_sql,
        )

        # Buffered writer for DuckLake
        table_configs: dict[str, DuckTableConfig] = {
            "cert_info": DuckTableConfig(
                model=CertInfoModel,
                mode=DuckWriteMode.INSERT_IGNORE,
                key_cols=["fingerprint"],
            ),
            "cert_observation": DuckTableConfig(
                model=CertObservationModel,
                mode=DuckWriteMode.APPEND,
            ),
            "dns_info": DuckTableConfig(
                model=DnsInfoModel,
                mode=DuckWriteMode.INSERT_IGNORE,
                key_cols=["hash"],
            ),
            "dns_observation": DuckTableConfig(
                model=DnsObservationModel,
                mode=DuckWriteMode.APPEND,
            ),
        }
        self._writer = DuckLakeBufferedWriter(
            lake=self._lake,
            table_configs=table_configs,
            row_thresh=row_thresh,
            time_thresh_sec=time_thresh_sec,
            jitter=jitter,
            log_helper=self.io_helper.log_helper,
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
            f"{self.__class__.__name__} initialized "
            f"(DuckLake at {catalog_path}, data {data_path}) "
            f"with {num_workers} worker(s)."
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

    def _cert_join_df(self, obs_df: pd.DataFrame, info_df: pd.DataFrame):
        return obs_df.merge(info_df, on="fingerprint", how="left")

    def _dns_join_df(self, obs_df: pd.DataFrame, info_df: pd.DataFrame):
        for col in self.DNS_RECORD_TYPES:
            if col not in obs_df.columns:
                obs_df[col] = None
        if info_df.empty:
            return obs_df
        wire_bytes_map = info_df.set_index("hash")["wire_bytes"]
        for col in [c for c in obs_df.columns if c.endswith("_semhash")]:
            obs_df[col.replace("_semhash", "")] = obs_df[col].map(
                wire_bytes_map
            )
        return obs_df.drop(
            columns=[c for c in obs_df.columns if c.endswith("_semhash")],
            errors="ignore"
        )

    def _handle_cert_result(self, result: dict, *, return_result: bool):
        cert_info = result.get("cert_info") or {}
        fingerprint = cert_info.get("fingerprint")
        if fingerprint:
            self._writer.add_row("cert_info", cert_info)

        cert_obs = {
            "timestamp": float(result.get("timestamp", time.time())),
            "host": result.get("host"),
            "port": int(result.get("port", 443)),
            "sni": result.get("sni"),
            "effective_host": result.get("effective_host"),
            "fingerprint": fingerprint,
            "valid": bool((result.get("cert_obs") or {}).get("valid", False)),
            "error": (result.get("cert_obs") or {}).get("error"),
        }
        self._writer.add_row("cert_observation", cert_obs)

        if not return_result:
            return None

        info_df = (
            pd.DataFrame.from_records(
                [cert_info], columns=list(CertInfoModel.model_fields.keys())
            ) if fingerprint else
            pd.DataFrame(columns=list(CertInfoModel.model_fields.keys()))
        )
        obs_df = pd.DataFrame.from_records(
            [cert_obs], columns=list(CertObservationModel.model_fields.keys())
        )
        return self._cert_join_df(obs_df, info_df)

    def _handle_dns_result(self, result: dict, *, return_result: bool):
        dns_info = result.get("dns_info") or {}
        if dns_info:
            self._writer.add_rows(
                "dns_info",
                [{"hash": h, "wire_bytes": b} for h, b in dns_info.items()],
            )

        dns_obs = result.get("dns_obs") or {}
        obs_row = {
            "timestamp": float(result.get("timestamp", time.time())),
            "host": result.get("host"),
        }
        for t in self.DNS_RECORD_TYPES:
            obs_row[f"{t}_semhash"] = dns_obs.get(f"{t}_semhash")
            obs_row[f"{t}_expiry"] = dns_obs.get(f"{t}_expiry")
        self._writer.add_row("dns_observation", obs_row)

        if not return_result:
            return None

        info_df = (
            pd.DataFrame.from_records(
                [{"hash": h, "wire_bytes": b} for h, b in dns_info.items()],
                columns=list(DnsInfoModel.model_fields.keys())
            ) if dns_info else
            pd.DataFrame(columns=list(DnsInfoModel.model_fields.keys()))
        )
        obs_df = pd.DataFrame.from_records(
            [obs_row], columns=list(DnsObservationModel.model_fields.keys())
        )
        return self._dns_join_df(obs_df, info_df)

    def _handle_cert_dns_result(
        self, result: tuple[dict, dict], *, return_result: bool
    ) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
        cert_result, dns_result = result
        maybe_cert_df = self._handle_cert_result(
            cert_result, return_result=return_result
        )
        maybe_dns_df = self._handle_dns_result(
            dns_result, return_result=return_result
        )
        return maybe_cert_df, maybe_dns_df

    async def grab_cert_async(
        self,
        host: str,
        port: int = 443,
        sni: Optional[str] = None,
        *,
        return_result: bool = True,
    ) -> pd.DataFrame | None:
        """
        Grab the TLS certificate from a host:port, store it in the database, and return the stored row if requested.
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

            return_result (bool):
                If True, return a single-row DataFrame with the obtained certificate information.
                If False, return None.
                Default is True.

        Returns:
            If `return_result` is True, a single-row DataFrame containing the joined
            certificate observation and info.
            If `return_result` is False, None.
        """
        result = await self._rpc(
            DomainProfileMethod.GET_CERT,
            host=host,
            port=port,
            sni=sni,
        )
        return await asyncio.to_thread(
            self._handle_cert_result, result, return_result=return_result
        )

    def grab_cert(
        self,
        host: str,
        port: int = 443,
        sni: Optional[str] = None,
        *,
        return_result: bool = True,
    ):
        """
        Synchronous version of `grab_cert_async()`.
        """
        return run_coro_sync(
            self.grab_cert_async(
                host, port, sni=sni,
                return_result=return_result
            )
        )

    async def grab_dns_async(
        self,
        host: str,
        *,
        return_result: bool = True
    ) -> pd.DataFrame | None:
        """
        Grab DNS records for a host and optionally return
        a single-row DataFrame of the observation joined with info.

        Args:
            host (str):
                The hostname to resolve.

            return_result (bool):
                If True, return a single-row DataFrame with the obtained DNS information.
                If False, return None.
                Default is True.

        Returns:
            If `return_result` is True, a single-row DataFrame containing the joined
            DNS observation and info.
            If `return_result` is False, None.
        """
        result = await self._rpc(DomainProfileMethod.GET_DNS, host=host)
        return await asyncio.to_thread(
            self._handle_dns_result, result, return_result=return_result
        )

    def grab_dns(self, host: str, *, return_result: bool = True):
        """
        Synchronous version of `grab_dns_async()`.
        """
        return run_coro_sync(self.grab_dns_async(host, return_result=return_result))

    async def grab_cert_dns_async(
        self,
        host: str,
        port: int = 443,
        *,
        return_result: bool = True,
    ) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
        """
        Grab both the TLS certificate and DNS records for a host
        and optionally return the joined DataFrames.

        Args:
            host (str):
                The hostname to connect to and resolve.

            port (int):
                The TCP port to connect to for the TLS certificate.
                Default is 443.

            return_result (bool):
                If True, return a tuple of DataFrames:
                (certificate observation + info, DNS observation + info).
                If False, return (None, None).
                Default is True.

        Returns:
            If `return_result` is True, a tuple of DataFrames containing the joined
            certificate observation + info and DNS observation + info.
            If `return_result` is False, (None, None).
        """
        result = await self._rpc(
            DomainProfileMethod.GET_CERT_DNS,
            host=host,
            port=port,
        )
        return await asyncio.to_thread(
            self._handle_cert_dns_result, result, return_result=return_result
        )

    def grab_cert_dns(
        self,
        host: str,
        port: int = 443,
        *,
        return_result: bool = True,
    ):
        """
        Synchronous version of `grab_cert_dns_async()`.
        """
        return run_coro_sync(self.grab_cert_dns_async(host, port, return_result=return_result))

    def get_all_certs(self):
        """
        Return a DataFrame of all stored certificate observations joined with info.
        """
        return self._lake.query_df("SELECT * FROM lake.cert_join_view;")

    def get_all_dns(self):
        """
        Return a DataFrame of all stored DNS observations joined with info.
        """
        return self._lake.query_df("SELECT * FROM lake.dns_join_view;")

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

        # Flush buffered rows before shutdown
        with contextlib.suppress(Exception):
            self._writer.close()
            self.io_helper.logger.info(
                "Flushed buffered rows before shutdown."
            )

        # Close DuckLake
        with contextlib.suppress(Exception):
            self._lake.close()

    def close(self):
        return run_coro_sync(self.aclose())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
