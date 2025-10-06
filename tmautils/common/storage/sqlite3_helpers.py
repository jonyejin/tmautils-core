from pathlib import Path
import multiprocessing as mp
from multiprocessing.connection import Connection
from ipaddress import ip_address
from logging import Logger
import pandas as pd
import threading
from queue import Queue, Empty
import io

from ..types import *
from ..ipc import *

if TYPE_CHECKING:
    from .sqlite3_storage import SqliteDatabase, SqliteTable  # noqa: F401 (type-only)


class SqliteLpmTrieHelper:
    """
    A helper class to build and manage a Longest Prefix Match (LPM) trie using PyTricia for fast IP address lookups in a SQLite database.
    This class uses multiprocessing to build the trie in a separate process, allowing for efficient lookups without blocking the main thread.
    Until the trie is ready, it falls back to a standard SQL query for lookups.
    See IpInfoPrivacyUtil for an example of how to use this class.

    Args:
        db_path (Path):
            The path to the SQLite database file.

        table (SqliteTable):
            The SqliteTable instance representing the table to query.

        version_col (str):
            The name of the column containing the IP version.
            Default is `version`.

        network_start_col (str):
            The name of the column containing the starting address of the network.
            Default is `network_start`.

        prefix_length_col (str):
            The name of the column containing the prefix length of the network.
            Default is `prefix_length`.

        logger (Logger | None):
            An optional logger instance for logging messages. If not provided, no logging will occur.
    """

    def __init__(
        self,
        db_path: Path,
        table: "SqliteTable",
        version_col: str = 'version',
        network_start_col: str = 'network_start',
        prefix_length_col: str = 'prefix_length',
        logger: Logger | None = None,
    ):
        self.db_path = db_path.expanduser().resolve(strict=True)
        self.table = table
        self.table_name = table.table_name
        self.key_cols = (version_col, network_start_col, prefix_length_col)
        self.logger = logger
        self._trie_pipe = None
        self._trie_ready = False
        self.trie4 = None
        self.trie6 = None

        self.start_trie_build_process()

        if self.logger:
            self.logger.info(
                f"Initialized LPM Trie helper for {self.table_name} with keys {self.key_cols}"
            )

    def start_trie_build_process(self):
        ctx = mp.get_context("spawn")
        self._trie_pipe, child_conn = ctx.Pipe(duplex=False)
        child = ctx.Process(
            target=self.__class__._build_process,
            args=(
                child_conn,
                str(self.db_path),
                self.table_name,
                self.key_cols,
            ),
            daemon=True,
        )
        child.start()
        if self.logger:
            self.logger.info(f"Started process {child.pid} to build LPM tries")

    @staticmethod
    def _build_process(
        pipe_conn: Connection,
        db_path: str,
        table_name: str,
        key_cols: tuple[str, ...],
    ):
        from ipaddress import ip_network
        from pytricia import PyTricia
        import pandas as pd
        from .sqlite3_storage import SqliteDatabase, _sqlite3_conversion_ctx

        # Gather primary keys from the database
        db = SqliteDatabase(db_path)
        with _sqlite3_conversion_ctx():
            df = pd.read_sql_query(
                f"SELECT {', '.join(key_cols)} FROM {table_name}",
                db.conn,
            )
        del db

        trie4 = PyTricia(32)
        trie6 = PyTricia(128)
        for row in df.itertuples(index=False):
            version, net_start, prefix_len = row
            net = ip_network((net_start, int(prefix_len)), strict=False)
            trie = trie4 if version == 4 else trie6
            trie[str(net)] = (version, net_start, prefix_len)

        # Freeze and send tries to the parent process
        trie4.freeze()
        trie6.freeze()
        pipe_conn.send(trie4)
        pipe_conn.send(trie6)
        pipe_conn.close()

    def _poll_ready(self):
        if self._trie_ready or not self._trie_pipe:
            # If already ready or pipe failed, do nothing
            return

        if self._trie_pipe.poll():
            try:
                # Receive the tries
                self.trie4 = self._trie_pipe.recv()
                self.trie6 = self._trie_pipe.recv()
                # Thaw the tries to make them usable
                self.trie4.thaw()
                self.trie6.thaw()
                self._trie_ready = True
                if self.logger:
                    self.logger.info("LPM tries ready")
            except EOFError:
                if self.logger:
                    self.logger.error("Failed to receive tries from the pipe")
            finally:
                self._trie_pipe.close()
                self._trie_pipe = None

    def lookup(self, ip: str | IPAddress) -> pd.Series:
        """
        Perform a lookup for the given IP address in the LPM trie or fall back to SQL query if the trie is not ready.

        Args:
            ip (str | IPAddress):
                The IP address to look up, either as a string or an IPAddress instance (IPv4Address or IPv6Address).

        Returns:
            pd.Series:
                A pandas Series containing the row from the table that matches the longest prefix match for the given IP address.
                If no match is found, an empty Series will be returned.
        """

        ip = ip_address(ip) if isinstance(ip, str) else ip

        # Poll the pipe to check if the trie is ready
        self._poll_ready()

        if self._trie_ready:
            # Use the trie for fast lookup
            trie = self.trie4 if ip.version == 4 else self.trie6
            pk_tuple = trie.get(str(ip))
            if pk_tuple is None:
                # If no match found, return empty Series
                return pd.Series(dtype=object)

            sql = f"""
                SELECT {', '.join(self.table.schema.keys())}
                FROM {self.table_name}
                WHERE version=? AND network_start=? AND prefix_length=?
            """

            return self.table.query(sql, pk_tuple).iloc[0]
        else:
            # Fallback to SQL query if trie is not ready
            sql = f"""
                SELECT {', '.join(self.table.schema.keys())}
                FROM {self.table_name}
                WHERE version = ?
                  AND network_start <= ?
                  AND network_end >= ?
                ORDER BY prefix_length DESC
                LIMIT 1
            """
            df = self.table.query(sql, (ip.version, ip, ip))
            return df.iloc[0] if not df.empty else pd.Series(dtype=object)


class SqliteWorkerMethod(IpcMethodBase, StrEnum):
    INIT = "init"
    REGISTER_TABLE = "register_table"
    INSERT_DF = "insert_df"
    QUERY_ALL = "query_all"
    QUERY = "query"
    SHUTDOWN = "shutdown"


class SqliteWorkerProcess:
    SERVICE = "sqlite_worker"

    def __init__(self, cmd_q: mp.Queue, rsp_q: mp.Queue):
        self.cmd_q = cmd_q
        self.rsp_q = rsp_q
        self.db: Optional[SqliteDatabase] = None
        self._initialized = False

        self.cmd_handlers: dict[SqliteWorkerMethod, Callable[..., Any]] = {
            SqliteWorkerMethod.INIT: self.handle_init,
            SqliteWorkerMethod.REGISTER_TABLE: self.handle_register_table,
            SqliteWorkerMethod.INSERT_DF: self.handle_insert_df,
            SqliteWorkerMethod.QUERY_ALL: self.handle_query_all,
            SqliteWorkerMethod.QUERY: self.handle_query,
            SqliteWorkerMethod.SHUTDOWN: self.handle_shutdown,
        }

    def handle_init(self, **kwargs):
        db_init_kwargs: dict = kwargs["db_init_kwargs"]
        # Do not re-offload
        db_init_kwargs["is_worker"] = True
        db_init_kwargs["offload_to_worker"] = False
        # Remove logger (we will log over IPC)
        db_init_kwargs["logger"] = None

        from .sqlite3_storage import SqliteDatabase
        self.db = SqliteDatabase(**db_init_kwargs)
        self._initialized = True
        return None

    def handle_register_table(self, **kwargs):
        table_def: dict = kwargs["table_def"]
        self.db.register_table(
            table_def["table_name"],
            table_def["schema"],
            qualifiers=table_def.get("qualifiers"),
            table_constraints=table_def.get("table_constraints"),
            indices=table_def.get("indices"),
        )
        return None

    def handle_insert_df(self, **kwargs):
        table_name: str = kwargs["table"]
        insert_kwargs: dict = kwargs.get("kwargs", {})
        df: pd.DataFrame = pd.read_pickle(io.BytesIO(kwargs["df"]))
        self.db.tables[table_name].insert_df(df, **insert_kwargs)
        return None

    def handle_query_all(self, **kwargs):
        table_name: str = kwargs["table"]
        out_df = self.db.tables[table_name].query_all()
        buf = io.BytesIO()
        out_df.to_pickle(buf)
        return buf.getvalue()

    def handle_query(self, **kwargs):
        table_name: str = kwargs["table"]
        sql: str = kwargs["sql"]
        query_kwargs: dict = kwargs.get("kwargs", {})
        out_df = self.db.tables[table_name].query(sql, **query_kwargs)
        buf = io.BytesIO()
        out_df.to_pickle(buf)
        return buf.getvalue()

    def handle_shutdown(self, **kwargs):
        if self.db:
            self.db.close()
            self.db = None
        return None

    def handle_request(self, msg: IpcMsg):
        if msg.service != self.SERVICE:
            resp = msg.respond_with(
                ok=False,
                error=RuntimeError(f"Unknown service '{msg.service}'"),
            )
            self.rsp_q.put(resp)
            return

        method = SqliteWorkerMethod.get_method(msg)

        handler = self.cmd_handlers.get(method)
        if handler is None:
            resp = msg.respond_with(
                ok=False,
                error=RuntimeError(f"Unknown method '{msg.method}'"),
            )
            self.rsp_q.put(resp)
            return

        if not self._initialized and method != SqliteWorkerMethod.INIT:
            resp = msg.respond_with(
                ok=False,
                error=RuntimeError("Worker needs to be initialized first"),
            )
            self.rsp_q.put(resp)
            return

        try:
            result = handler(**(msg.kwargs or {}))
            self.rsp_q.put(msg.respond_with(ok=True, result=result))
        except Exception as e:
            self.rsp_q.put(msg.respond_with(ok=False, error=e))

    def run(self):
        while True:
            try:
                msg = self.cmd_q.get()
            except KeyboardInterrupt:
                continue  # Wait for shutdown command
            except (EOFError, BrokenPipeError, OSError):
                break  # Something horrible happened

            if not isinstance(msg, IpcMsg) or not msg.is_request:
                continue

            self.handle_request(msg)

            # Exit loop on shutdown command
            if SqliteWorkerMethod.get_method(msg) is SqliteWorkerMethod.SHUTDOWN:
                break


class SqliteWorkerHelper:
    QUEUE_MAX_SIZE = 10_000
    RPC_TIMEOUT = 30.0
    RPC_TIMEOUT_SQL = 120.0
    RPC_TIMEOUT_SHUTDOWN = 10.0

    SERVICE = SqliteWorkerProcess.SERVICE

    def __init__(self, db_init_kwargs: dict):
        self.db_init_kwargs = dict(db_init_kwargs)

        # Remove logger from init kwargs (not picklable)
        self.logger: Optional[Logger] = self.db_init_kwargs.pop("logger", None)

        self._ctx = mp.get_context("spawn")
        self.cmd_q = self._ctx.Queue(maxsize=self.QUEUE_MAX_SIZE)
        self.rsp_q = self._ctx.Queue()
        self.worker_proc: Optional[mp.Process] = None

        self.req_id = 0
        self.req_lock = threading.Lock()
        self.waiters: Dict[int, Queue[IpcMsg]] = {}
        self.waiters_lock = threading.Lock()

        self.resp_reader_thread = threading.Thread(
            target=self._resp_read_loop,
            name=f"{self.__class__.__name__}-reader",
            daemon=True,
        )
        self._closed = False

    def _next_id(self) -> int:
        with self.req_lock:
            rid = self.req_id
            self.req_id += 1
            return rid

    @staticmethod
    def _child_entry(cmd_q: mp.Queue, rsp_q: mp.Queue):
        # Ignore SIGINT in the child process to avoid KeyboardInterrupt
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        worker = SqliteWorkerProcess(cmd_q, rsp_q)
        worker.run()

    def _resp_read_loop(self):
        while not self._closed:
            try:
                msg = self.rsp_q.get(timeout=0.5)
            except Empty:
                # Timeout, loop again
                continue
            except (EOFError, BrokenPipeError, OSError) as e:
                if self.logger:
                    self.logger.error(
                        f"Response queue closed/broken: {e}. Exiting reader thread."
                    )
                break
            except Exception as e:
                if self.logger:
                    self.logger.error(
                        f"Error reading from response queue: {e}"
                    )
                continue

            if not isinstance(msg, IpcMsg) or not msg.is_response or msg.req_id is None:
                if self.logger:
                    self.logger.error(
                        f"Received invalid message on response queue, ignoring: {msg}"
                    )
                continue

            with self.waiters_lock:
                q = self.waiters.get(msg.req_id)
            if q is not None:
                q.put(msg)
            else:
                pass  # No waiter for this message ID

    def _rpc(
        self,
        method: SqliteWorkerMethod,
        payload: dict,
        *,
        block: bool = True,
        timeout: Optional[float] = None,
    ):
        req_id = self._next_id()
        req = IpcMsg.request(
            req_id=req_id,
            service=self.SERVICE,
            method=method,
            **payload,
        )

        if block:
            q: Queue[IpcMsg] = Queue(maxsize=1)
            with self.waiters_lock:
                self.waiters[req_id] = q

        self.cmd_q.put(req)
        if not block:
            return None

        try:
            resp = q.get(timeout=timeout)
            if not resp.ok:
                raise_from_payload(resp.error)
            return resp
        except Empty:
            raise TimeoutError(
                f"Blocing RPC call {method} timed out after {timeout} seconds"
            )
        finally:
            with self.waiters_lock:
                self.waiters.pop(req_id, None)

    def start(self):
        if self.worker_proc and self.worker_proc.is_alive():
            return

        self.worker_proc = self._ctx.Process(
            target=self._child_entry,
            args=(self.cmd_q, self.rsp_q),
            daemon=False,
        )
        self.worker_proc.start()
        self.resp_reader_thread.start()

        # Initialize the worker (blocking)
        self._rpc(
            SqliteWorkerMethod.INIT,
            {"db_init_kwargs": self.db_init_kwargs},
            block=True,
            timeout=self.RPC_TIMEOUT,
        )

    def register_table(self, table_def: dict):
        resp = self._rpc(
            SqliteWorkerMethod.REGISTER_TABLE,
            {"table_def": table_def},
            block=True,
            timeout=self.RPC_TIMEOUT,
        )

    def insert_df(
        self,
        table_name: str,
        df: pd.DataFrame,
        block: bool = False,
        **kwargs
    ):
        buf = io.BytesIO()
        df.to_pickle(buf)
        payload = {
            "table": table_name,
            "df": buf.getvalue(),
            "kwargs": kwargs,
        }

        self._rpc(
            SqliteWorkerMethod.INSERT_DF,
            payload,
            block=block,
            timeout=self.RPC_TIMEOUT_SQL if block else None,
        )

    def query_all(self, table_name: str) -> pd.DataFrame:
        resp = self._rpc(
            SqliteWorkerMethod.QUERY_ALL,
            {"table": table_name},
            block=True,
            timeout=self.RPC_TIMEOUT_SQL,
        )
        return pd.read_pickle(io.BytesIO(resp.result))

    def query(self, table_name: str, sql: str, **kwargs) -> pd.DataFrame:
        resp = self._rpc(
            SqliteWorkerMethod.QUERY,
            {"table": table_name, "sql": sql, "kwargs": kwargs},
            block=True,
            timeout=self.RPC_TIMEOUT_SQL,
        )
        return pd.read_pickle(io.BytesIO(resp.result))

    def close(self):
        if self._closed:
            return

        try:
            self._rpc(
                SqliteWorkerMethod.SHUTDOWN,
                {},
                block=True,
                timeout=self.RPC_TIMEOUT_SHUTDOWN,
            )
        except Exception:
            if self.worker_proc and self.worker_proc.is_alive():
                # Couldn't shutdown cleanly, force terminate
                self.worker_proc.terminate()

        self._closed = True
        if self.worker_proc:
            self.worker_proc.join(timeout=self.RPC_TIMEOUT_SHUTDOWN)
        if self.resp_reader_thread.is_alive():
            self.resp_reader_thread.join(timeout=1.0)
