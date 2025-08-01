from pathlib import Path
from multiprocessing import Process, Pipe
from multiprocessing.connection import Connection
from ipaddress import ip_address
from logging import Logger
import pandas as pd

from .sqlite3_storage import SqliteTable
from ..types import IPAddress


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
        table: SqliteTable,
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
        self._trie_pipe, child_conn = Pipe(duplex=False)
        child = Process(
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
