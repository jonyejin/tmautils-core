from typing import Any, Dict, Optional, Iterable, Tuple
import contextlib
import duckdb
import pyarrow as pa
from uuid import uuid4
import threading
from dataclasses import dataclass

from tmautils.common import LogHelper, get_logger_from_helper
from .base import (
    _quote_ident, _validate_write_mode,
    ArrowBackend, WriteMode, TableConfig,
)


class DuckDbStore:
    """
    Helper class to manage a DuckDB database connection and perform operations.
    For Ducklake, use DuckLakeStore instead.

    Args:
        db_path (str):
            Path to the DuckDB database file.

        connect_kwargs (Optional[Dict[str, Any]]):
            Additional keyword arguments to pass to duckdb.connect().

        log_helper (Optional[LogHelper]):
            Optional LogHelper instance for logging.
    """

    def __init__(
        self,
        db_path: str,
        *,
        connect_kwargs: Dict[str, Any] | None = None,
        log_helper: Optional[LogHelper] = None,
    ):
        import os

        self._register_lock = threading.Lock()
        self._log_helper = log_helper
        self.logger = get_logger_from_helper(self._log_helper)

        self.con: duckdb.DuckDBPyConnection = duckdb.connect(
            db_path,
            **(connect_kwargs or {
                "config": {
                    "threads": max(1, os.cpu_count() // 2),
                }
            })
        )

        self.logger.info(f"DuckDbStore initialized with db_path={db_path}")

    def execute(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
    ):
        """
        Execute a SQL statement.
        This method does not return any results.

        Args:
            sql (str):
                The SQL statement to execute.

            params (Optional[Iterable[Any]]):
                Optional parameters for the SQL statement.
        """

        cur = self.con.cursor()
        try:
            cur.execute(sql, params or ())
        finally:
            cur.close()

    def execute_txn(
        self,
        statements: Iterable[Tuple[str, Iterable[Any] | None]],
    ):
        """
        Execute multiple SQL statements in a single transaction.
        This method will commit the transaction if all statements succeed,
        or roll back if any statement fails.
        This method does not return any results.

        Args:
            statements (Iterable[Tuple[str, Iterable[Any] | None]]):
                An iterable of SQL statements and their optional parameters.
        """

        cur = self.con.cursor()
        try:
            cur.execute("BEGIN")
            for sql, params in statements:
                cur.execute(sql, params or ())
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def query_records(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
    ):
        """
        Execute a SQL query and return the results as a list of dictionaries.
        """

        cur = self.con.cursor()
        try:
            rel = cur.query(sql, params=params or ())
            cols = rel.columns
            rows = rel.fetchall()
            return [dict(zip(cols, r)) for r in rows]
        finally:
            cur.close()

    def query_one(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
    ):
        """
        Execute a SQL query and return a single value (first column of the first row).
        """

        cur = self.con.cursor()
        try:
            rel = cur.query(sql, params=params or ())
            v = rel.fetchone()
            return v[0] if v else None
        finally:
            cur.close()

    def query_arrow(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        batch_size: int = 1_000_000,
    ):
        """
        Execute a SQL query and return the results as a PyArrow Table.

        Args:
            sql (str):
                The SQL query to execute.

            params (Optional[Iterable[Any]]):
                Optional parameters for the SQL query.

            batch_size (int):
                The batch size for fetching Arrow data (default: 1,000,000).

        Returns:
            A pyarrow.RecordBatchReader instance for the query results.
        """

        cur = self.con.cursor()
        try:
            return cur.query(
                sql,
                params=params or ()
            ).arrow(
                batch_size=batch_size
            )
        finally:
            cur.close()

    def query_df(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        date_as_object: bool = False,
    ):
        """
        Execute a SQL query and return the results as a Pandas DataFrame.
        """

        cur = self.con.cursor()
        try:
            return cur.query(
                sql,
                params=params or ()
            ).df(
                date_as_object=date_as_object
            )
        finally:
            cur.close()

    @contextlib.contextmanager
    def _temp_view(self, vname: str, table: pa.Table):
        cur = self.con.cursor()
        with self._register_lock:
            cur.register(vname, table)
        try:
            yield cur
        finally:
            with contextlib.suppress(Exception):
                with self._register_lock:
                    cur.unregister(vname)
            with contextlib.suppress(Exception):
                cur.close()

    def insert_arrow(
        self,
        table: str,
        data: pa.Table,
        *,
        mode: WriteMode = WriteMode.APPEND,
        key_cols: list[str] | None = None,
    ):
        """
        Insert data from a PyArrow Table into a DuckDB table.

        Args:
            table (str):
                The target table name.

            data (pa.Table):
                The PyArrow Table containing the data to insert.

            mode (WriteMode):
                The insert mode. Default is WriteMode.APPEND.

            key_cols (Optional[List[str]]):
                The key columns for "insert_ignore" and "upsert" modes.
        """
        if data is None or data.num_rows == 0:
            return

        _validate_write_mode(mode, table=data, key_cols=key_cols)

        fq = _quote_ident(table)
        vname = f"__incoming_{uuid4().hex}"

        with self._temp_view(vname, data) as cur:
            cur.execute("BEGIN")
            try:
                if mode == WriteMode.APPEND:
                    cur.execute(
                        f"INSERT INTO {fq} BY NAME SELECT * FROM {vname};")

                elif mode == WriteMode.INSERT_IGNORE:
                    keys_csv = ", ".join(_quote_ident(k) for k in key_cols)
                    cur.execute(f"""
                        INSERT INTO {fq} BY NAME
                        SELECT * FROM {vname}
                        ON CONFLICT ({keys_csv}) DO NOTHING;
                    """)

                elif mode == WriteMode.UPSERT:
                    incoming_cols = list(data.schema.names)
                    non_keys = [c for c in incoming_cols if c not in key_cols]
                    keys_csv = ", ".join(_quote_ident(k) for k in key_cols)

                    if not non_keys:
                        # Only keys -> behave like INSERT_IGNORE
                        cur.execute(f"""
                            INSERT INTO {fq} BY NAME
                            SELECT * FROM {vname}
                            ON CONFLICT ({keys_csv}) DO NOTHING;
                        """)
                    else:
                        # Update non-key columns on conflict
                        sets = ", ".join(
                            f"{_quote_ident(c)} = EXCLUDED.{_quote_ident(c)}"
                            for c in non_keys
                        )
                        cur.execute(f"""
                            INSERT INTO {fq} BY NAME
                            SELECT * FROM {vname}
                            ON CONFLICT ({keys_csv}) DO UPDATE SET {sets};
                        """)

                cur.execute("COMMIT")
                self.logger.debug(
                    "Inserted %d rows into '%s' (mode='%s')",
                    data.num_rows, fq, mode
                )
            except Exception:
                with contextlib.suppress(Exception):
                    cur.execute("ROLLBACK")
                raise

    def close(self):
        """Close the DuckDB connection"""
        with contextlib.suppress(Exception):
            self.con.close()
        self.logger.info("DuckDbStore closed.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@dataclass
class DuckDbBackend(ArrowBackend):
    """
    ArrowBackend implementation for DuckDB.

    Args:
        store (DuckDbStore):
            The DuckDbStore instance to use.
    """
    store: DuckDbStore

    def flush_arrow(
        self,
        table_name: str,
        config: TableConfig,
        data: pa.Table,
    ) -> None:
        self.store.insert_arrow(
            table_name,
            data,
            mode=config.mode,
            key_cols=config.key_cols,
        )
