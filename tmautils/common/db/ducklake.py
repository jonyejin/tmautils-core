import math
import contextlib
import duckdb
import pyarrow as pa
from pydantic import BaseModel

from ..types import *
from ..io import LogHelper, get_logger_from_helper

_T = TypeVar("_T")


def _quote_ident(name: str):
    if not isinstance(name, str):
        raise TypeError("identifier must be a string")
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(s: str) -> str:
    if not isinstance(s, str):
        raise TypeError("literal must be a string")
    return "'" + s.replace("'", "''") + "'"


def _to_arrow_array_ts(values, typ: pa.DataType) -> pa.Array:
    unit = typ.unit  # e.g., 's', 'ms', 'us', 'ns'
    scale = {
        's': 1, 'ms': 1_000, 'us': 1_000_000, 'ns': 1_000_000_000
    }[unit]
    conv: List[Optional[int]] = []
    for v in values:
        if v is None:
            conv.append(None)
        elif isinstance(v, (int, float)):
            if not math.isfinite(v):
                conv.append(None)
            else:
                conv.append(int(round(float(v) * scale)))
        else:
            # Fix your data!
            raise ValueError(
                f"Cannot convert value '{v}' of type '{type(v)}' to timestamp: "
                f"expected int/float epoch seconds or None."
            )
    return pa.array(conv, type=typ)


def _to_arrow_array(values, typ: pa.DataType) -> pa.Array:
    if pa.types.is_timestamp(typ):
        return _to_arrow_array_ts(values, typ)
    return pa.array(values, type=typ)


def _verify_names_match(model_cls: Type[BaseModel], sch: pa.Schema):
    model_fields = list(model_cls.model_fields.keys())
    schema_fields = [f.name for f in sch]
    if set(model_fields) != set(schema_fields):
        raise ValueError(
            "Pydantic model field names do not match ARROW_SCHEMA.\n"
            f"Model fields: {sorted(model_fields)}\n"
            f"Schema fields: {sorted(schema_fields)}"
        )


def pydantic_to_arrow(
    items: List[BaseModel],
    model_cls: Type[BaseModel]
) -> Optional[pa.Table]:
    """
    Convert a list of Pydantic model instances to a PyArrow Table.

    Args:
        items (List[BaseModel]):
            List of Pydantic model instances to convert.

        model_cls (Type[BaseModel]):
            The Pydantic model class corresponding to the instances.

    Returns:
        A PyArrow Table representing the data, or None if the input list is empty.
    """
    if not items:
        return None

    # Extract schema and do sanity checks
    if not hasattr(model_cls, "ARROW_SCHEMA"):
        raise ValueError("Model must have ARROW_SCHEMA attribute")
    schema: pa.Schema = model_cls.ARROW_SCHEMA
    if not isinstance(schema, pa.Schema):
        raise ValueError(
            "Model.ARROW_SCHEMA must be a pyarrow.Schema instance"
        )
    _verify_names_match(model_cls, schema)

    cols = []
    for field in schema:
        name = field.name
        vals = [getattr(item, name) for item in items]
        cols.append(_to_arrow_array(vals, field.type))
    return pa.Table.from_arrays(cols, schema=schema)


class DuckLakeStore:
    """
    Helper class to work with DuckLake data lakes via DuckDB.
    Provides methods to attach/detach lakes, execute queries, insert data,
    and perform checkpointing with retry logic for transient errors.

    Args:
        connection (Optional[DuckDBPyConnection]):
            An existing DuckDB connection to use. If not provided, a new connection
            will be created.

        connect_kwargs (Optional[Dict[str, Any]]):
            Keyword arguments to pass when creating a new DuckDB connection.

        retry_attempts (int):
            Number of retry attempts for transient errors (default: 10).

        retry_multiplier (float):
            Multiplier for exponential backoff between retries (default: 0.5).

        retry_max_wait (float):
            Maximum wait time between retries in seconds (default: 30).

        log_helper (Optional[LogHelper]):
            Optional logging helper for logging messages.
    """

    RETRY_ATTEMPTS = 10
    RETRY_MULTIPLIER = 0.5
    RETRY_MAX_WAIT = 30

    def __init__(
        self,
        connection: Optional[duckdb.DuckDBPyConnection] = None,
        *,
        connect_kwargs: Dict[str, Any] | None = None,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_multiplier: float = RETRY_MULTIPLIER,
        retry_max_wait: float = RETRY_MAX_WAIT,
        log_helper: Optional[LogHelper] = None,
    ):
        import os
        import threading

        self._lakes: list[str] = []
        self._lakes_lock = threading.Lock()
        self._register_lock = threading.Lock()

        self._log_helper = log_helper
        self.logger = get_logger_from_helper(self._log_helper)

        self.retry_attempts = retry_attempts
        self.retry_multiplier = retry_multiplier
        self.retry_max_wait = retry_max_wait

        self.con: duckdb.DuckDBPyConnection = (
            connection or duckdb.connect(**(
                connect_kwargs or {
                    "config": {
                        "threads": max(1, os.cpu_count() // 2),
                    }
                }
            ))
        )

        self.con.execute("INSTALL ducklake; LOAD ducklake;")

        self.logger.info("DuckLakeStore initialized.")

    @staticmethod
    def _is_transient_error(e: Exception):
        try:
            msg = str(e).lower()
        except Exception:
            msg = ""
        return any(
            ind in msg for ind in (
                "locked", "busy", "timeout", "could not acquire",
                "rollback", "deadlock",
            )
        )

    def run_with_retry(self, fn: Callable[..., _T], *args, **kwargs) -> _T:
        """
        Run a function with retry logic for DuckDB transient lock errors.
        Retry behavior is controlled by the instance's retry settings.

        Args:
            fn (Callable[..., _T]):
                The function to execute.

            *args:
                Positional arguments to pass to the function.

            **kwargs:
                Keyword arguments to pass to the function.

        Returns:
            The result of the function execution.
        """

        import tenacity

        for attempt in tenacity.Retrying(
            stop=tenacity.stop_after_attempt(self.retry_attempts),
            wait=tenacity.wait_random_exponential(
                multiplier=self.retry_multiplier,
                max=self.retry_max_wait,
            ),
            retry=tenacity.retry_if_exception(self._is_transient_error),
            reraise=True,
        ):
            with attempt:
                return fn(*args, **kwargs)

    def attach_lake(
        self,
        alias: str,
        catalog_path: str,
        *,
        data_path: str | None = None,
        options: list[str] | None = None,
        extensions: Tuple[str, ...] | None = None,
        schema_sql: str | None = None,
        retry_on_lock: bool = True,
    ):
        """
        Attach a DuckLake lake to the DuckDB connection.

        Args:
            alias (str):
                The alias to use for the attached lake.

            catalog_path (str):
                The path to the DuckLake catalog.

            data_path (Optional[str]):
                Optional data path for the lake.

            options (Optional[List[str]]):
                Optional list of additional options for the ATTACH command.
                e.g., `["META_JOURNAL_MODE 'WAL'", "META_BUSY_TIMEOUT 500"]`.

            extensions (Optional[Tuple[str, ...]]):
                Optional tuple of DuckDB extensions to install and load
                before attaching the lake.

            schema_sql (Optional[str]):
                Optional SQL string to execute after attaching the lake,
                for setting up schema (e.g., creating tables).
                The placeholder "{{alias}}" in the SQL will be replaced
                with the actual lake alias.

            retry_on_lock (bool):
                Whether to retry on transient lock errors (default: True).
        """

        def _once():
            with self._lakes_lock:
                if alias in self._lakes:
                    self.logger.info(
                        f"DuckLake '{alias}' is already attached; skipping attach."
                    )
                    return

            cur = self.con.cursor()  # For thread safety

            if extensions:
                for ext in extensions:
                    cur.execute(f"INSTALL {ext}; LOAD {ext};")

            # Attach lake
            try:
                cur.execute("BEGIN")

                opt_parts = []
                if data_path:
                    opt_parts.append(f"DATA_PATH '{data_path}'")
                if options:
                    opt_parts.extend(options)
                options_sql = f" ({', '.join(opt_parts)})" if opt_parts else ""
                alias_ident = _quote_ident(alias)
                cur.execute(
                    f"ATTACH 'ducklake:{catalog_path}' AS {alias_ident}{options_sql};"
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

            with self._lakes_lock:
                self._lakes.append(alias)

            # Apply schema SQL if provided
            if schema_sql:
                schema_sql_loc = schema_sql.replace("{{alias}}", alias)
                cur = self.con.cursor()
                try:
                    cur.execute("BEGIN")
                    cur.execute(schema_sql_loc)
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise
                finally:
                    cur.close()

            self.logger.info(
                f"DuckLake '{alias}' attached with catalog_path='{catalog_path}', "
                f"data_path='{data_path}' and options='{options}'."
            )

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def detach_lake(
        self,
        alias: Optional[str] = None,
        *,
        retry_on_lock: bool = True,
    ):
        """
        Detach a DuckLake lake from the DuckDB connection.

        Args:
            alias (Optional[str]):
                The alias of the lake to detach. If not provided, and only one lake
                is attached, that lake will be detached.

            retry_on_lock (bool):
                Whether to retry on transient lock errors (default: True).
        """

        alias = alias or self._get_default_alias()

        def _once():
            with self._lakes_lock:
                if alias not in self._lakes:
                    self.logger.info(
                        f"DuckLake '{alias}' is not attached; skipping detach."
                    )
                    return

            cur = self.con.cursor()
            try:
                alias_ident = _quote_ident(alias)
                cur.execute(f"DETACH {alias_ident};")
            finally:
                cur.close()

            with self._lakes_lock:
                self._lakes.remove(alias)

            self.logger.info(f"DuckLake '{alias}' detached.")

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def _get_default_alias(self) -> str:
        with self._lakes_lock:
            n = len(self._lakes)
            first = self._lakes[0] if n else None

        if not n:
            raise RuntimeError("No DuckLake lake attached.")
        if n > 1:
            raise RuntimeError(
                "Multiple DuckLake lakes attached; please specify lake alias."
            )
        return first

    def execute(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        retry_on_lock: bool = True,
    ):
        """
        Execute a SQL statement.

        Args:
            sql (str):
                The SQL statement to execute.

            params (Optional[Iterable[Any]]):
                Optional parameters for the SQL statement.

            retry_on_lock (bool):
                Whether to retry on transient lock errors (default: True).
        """

        def _once():
            cur = self.con.cursor()
            try:
                cur.execute(sql, params or ())
            finally:
                cur.close()

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def execute_txn(
        self,
        statements: Iterable[Tuple[str, Iterable[Any] | None]],
        *,
        retry_on_lock: bool = True,
    ):
        """
        Execute multiple SQL statements in a single transaction.

        Args:
            statements (Iterable[Tuple[str, Iterable[Any] | None]]):
                An iterable of SQL statements and their optional parameters.

            retry_on_lock (bool):
                Whether to retry on transient lock errors (default: True).
        """

        def _once():
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

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def query_records(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        retry_on_lock: bool = True,
    ):
        """
        Execute a SQL query and return the results as a list of dictionaries.
        """

        def _once():
            cur = self.con.cursor()
            try:
                rel = cur.query(sql, params=params or ())
                cols = rel.columns
                rows = rel.fetchall()
                return [dict(zip(cols, r)) for r in rows]
            finally:
                cur.close()
        return self.run_with_retry(_once) if retry_on_lock else _once()

    def query_one(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        retry_on_lock: bool = True,
    ):
        """
        Execute a SQL query and return a single value (first column of the first row).
        """

        def _once():
            cur = self.con.cursor()
            try:
                rel = cur.query(sql, params=params or ())
                v = rel.fetchone()
                return v[0] if v else None
            finally:
                cur.close()

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def query_arrow(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        batch_size: int = 1_000_000,
        retry_on_lock: bool = True,
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

            retry_on_lock (bool):
                Whether to retry on transient lock errors (default: True).

        Returns:
            A pyarrow.RecordBatchReader instance for the query results.
        """

        def _once():
            cur = self.con.cursor()
            try:
                return cur.query(sql, params=params or ()).arrow(
                    batch_size=batch_size
                )
            finally:
                cur.close()

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def query_df(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
        *,
        date_as_object: bool = False,
        retry_on_lock: bool = True,
    ):
        """
        Execute a SQL query and return the results as a Pandas DataFrame.
        """

        def _once():
            cur = self.con.cursor()
            try:
                return cur.query(sql, params=params or ()).df(
                    date_as_object=date_as_object
                )
            finally:
                cur.close()

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def snapshot(
        self,
        lake: Optional[str] = None,
        *,
        retry_on_lock: bool = True,
    ):
        """
        Get the current snapshot id for a DuckLake lake.
        """
        alias = lake or self._get_default_alias()
        alias_ident = _quote_ident(alias)
        return self.query_one(
            f"SELECT * FROM {alias_ident}.current_snapshot();",
            retry_on_lock=retry_on_lock,
        )

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
        table: str, data: pa.Table,
        *,
        mode: str = "append", key_cols: list[str] | None = None,
        lake: str | None = None,
        retry_on_lock: bool = True,
    ):
        """
        Insert data from a PyArrow Table into a DuckLake table.

        Args:
            table (str):
                The target table name.

            data (pa.Table):
                The PyArrow Table containing the data to insert.

            mode (str):
                The insert mode: "append", "insert_ignore", or "upsert" (default: "append").

            key_cols (Optional[List[str]]):
                The key columns for "insert_ignore" and "upsert" modes.

            lake (Optional[str]):
                The lake alias to use. If not provided, the default attached lake is used.

            retry_on_lock (bool):
                Whether to retry on transient lock errors (default: True).
        """
        def _once():
            from uuid import uuid4
            if data is None or data.num_rows == 0:
                return

            supported_modes = {"append", "insert_ignore", "upsert"}
            if mode not in supported_modes:
                raise ValueError(
                    f"Unsupported mode '{mode}'; must be one of {supported_modes}"
                )
            if mode in {"insert_ignore", "upsert"} and not key_cols:
                raise ValueError(f"mode='{mode}' requires key_cols")

            alias = lake or self._get_default_alias()
            fq = f"{_quote_ident(alias)}.{_quote_ident(table)}"
            vname = f"__incoming_{uuid4().hex}"

            with self._temp_view(vname, data) as cur:
                cur.execute("BEGIN")
                try:
                    if mode == "append":
                        cur.execute(
                            f"INSERT INTO {fq} BY NAME SELECT * FROM {vname};"
                        )

                    # Common parts for insert_ignore and upsert
                    if mode in {"insert_ignore", "upsert"}:
                        incoming_cols = list(data.schema.names)
                        keys_csv = ", ".join(_quote_ident(k) for k in key_cols)
                        proj_csv = ", ".join(
                            [f"i.{_quote_ident(c)}" for c in incoming_cols]
                        )
                        src = f"""(
                            SELECT * FROM (
                                SELECT *, ROW_NUMBER() OVER (
                                    PARTITION BY {keys_csv}) AS __rn
                                    FROM {vname}
                                )
                                WHERE __rn = 1
                        )"""

                    if mode == "insert_ignore":
                        cur.execute(f"""
                            INSERT INTO {fq} BY NAME
                            SELECT {proj_csv}
                            FROM {src} i
                            ANTI JOIN {fq} t USING ({keys_csv});
                        """)
                    elif mode == "upsert":
                        non_keys = [
                            c for c in incoming_cols if c not in key_cols]
                        if not non_keys:
                            # Only keys -> behave like insert_ignore against the target
                            cur.execute(f"""
                                INSERT INTO {fq} BY NAME
                                SELECT {proj_csv}
                                FROM {src} i
                                ANTI JOIN {fq} t USING ({keys_csv});
                            """)
                        else:
                            on = " AND ".join(
                                [f"t.{_quote_ident(k)}=i.{_quote_ident(k)}" for k in key_cols]
                            )
                            sets = ", ".join(
                                [f"{_quote_ident(c)}=i.{_quote_ident(c)}" for c in non_keys]
                            )
                            cols_csv = ", ".join(
                                _quote_ident(c) for c in incoming_cols
                            )
                            vals_csv = ", ".join(
                                [f"i.{_quote_ident(c)}" for c in incoming_cols]
                            )
                            cur.execute(f"""
                                MERGE INTO {fq} t
                                USING {src} i
                                ON {on}
                                WHEN MATCHED THEN UPDATE SET {sets}
                                WHEN NOT MATCHED THEN INSERT ({cols_csv}) VALUES ({vals_csv});
                            """)

                    cur.execute("COMMIT")
                    self.logger.debug(
                        f"Inserted {data.num_rows} rows into '{fq}' (mode='{mode}')."
                    )
                except Exception:
                    with contextlib.suppress(Exception):
                        cur.execute("ROLLBACK")
                    raise

        return self.run_with_retry(_once) if retry_on_lock else _once()

    def insert_records(
        self,
        table: str, items: List[Dict[str, Any]], model_cls: Type[BaseModel],
        *,
        mode: str = "append", key_cols: list[str] | None = None,
        lake: Optional[str] = None,
        retry_on_lock: bool = True,
    ):
        """
        Insert data from a list of dictionaries into a DuckLake table.

        Args:
            table (str):
                The target table name.

            items (List[Dict[str, Any]]):
                The list of dictionaries containing the data to insert.

            model_cls (Type[BaseModel]):
                The Pydantic model class corresponding to the data.

            mode (str):
                The insert mode: "append", "insert_ignore", or "upsert" (default: "append").

            key_cols (Optional[List[str]]):
                The key columns for "insert_ignore" and "upsert" modes.

            lake (Optional[str]):
                The lake alias to use. If not provided, the default attached lake is used.

            retry_on_lock (bool):
                Whether to retry on transient lock errors (default: True).
        """
        t = pydantic_to_arrow(
            [model_cls(**item) for item in items],
            model_cls
        )
        self.insert_arrow(
            table, t,
            mode=mode,
            key_cols=key_cols,
            lake=lake,
            retry_on_lock=retry_on_lock,
        )

    def checkpoint(
        self,
        *,
        lake: Optional[str] = None,
        compaction_schema: Optional[str] = None,
        compaction_table: Optional[str] = None,
        expire_older_than: Optional[str] = None,
        delete_older_than: Optional[str] = None,
        rewrite_delete_threshold: Optional[float] = None,
        retry_on_lock: bool = True,
    ):
        """
        Perform a checkpoint on the specified DuckLake lake.
        For options, see DuckLake documentation.
        """

        alias = lake or self._get_default_alias()
        alias_ident = _quote_ident(alias)

        stmts: List[Tuple[str, Optional[Iterable[Any]]]] = []
        if compaction_schema:
            stmts.append((
                f"CALL {alias_ident}.set_option('compaction_schema', "
                f"{_quote_literal(compaction_schema)});",
                None
            ))
        if compaction_table:
            stmts.append((
                f"CALL {alias_ident}.set_option('compaction_table', "
                f"{_quote_literal(compaction_table)});",
                None
            ))
        if expire_older_than:
            stmts.append((
                f"CALL {alias_ident}.set_option('expire_older_than', "
                f"{_quote_literal(expire_older_than)});",
                None
            ))
        if delete_older_than:
            stmts.append((
                f"CALL {alias_ident}.set_option('delete_older_than', "
                f"{_quote_literal(delete_older_than)});",
                None
            ))
        if rewrite_delete_threshold is not None:
            stmts.append((
                f"CALL {alias_ident}.set_option('rewrite_delete_threshold', "
                f"{rewrite_delete_threshold});",
                None
            ))
        stmts.append(("CHECKPOINT;", None))
        self.execute_txn(stmts, retry_on_lock=retry_on_lock)

    def close(self):
        """
        Close the DuckLakeStore and its DuckDB connection.
        """
        with contextlib.suppress(Exception):
            self.con.close()
        with self._lakes_lock:
            self._lakes = []
        self.logger.info("DuckLakeStore closed.")

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, tb): self.close()
