from ipaddress import IPv4Address, IPv6Address
from typing import Type, Any, Callable
import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from logging import Logger
from contextlib import contextmanager

from ..utils import try_convert_ip


# Context-manager to locally register adapters/converters
@contextmanager
def _sqlite3_conversion_ctx():
    # Snapshot
    orig_adapters = sqlite3.adapters.copy()
    orig_converters = sqlite3.converters.copy()
    try:
        # Adapters (Python -> SQLite)
        for py_type, adapter in SqliteTable.SQLITE3_ADAPTERS.items():
            sqlite3.register_adapter(py_type, adapter)
        # Converters (SQLite -> Python)
        for sql_type, converter in SqliteTable.SQLITE3_CONVERTERS.items():
            sqlite3.register_converter(sql_type, converter)
        yield
    finally:
        # Restore
        sqlite3.adapters.clear()
        sqlite3.adapters.update(orig_adapters)
        sqlite3.converters.clear()
        sqlite3.converters.update(orig_converters)


class SqliteTable:
    """
    A class to manage SQLite tables with a defined schema, including qualifiers, constraints, and indices.
    This class provides methods to create or verify a table, insert data from a pandas DataFrame,
    and query data using SQL and obtain results as a DataFrame.
    It tries to verify that the table schema matches the provided schema.

    Args:
        conn (sqlite3.Connection):
            SQLite connection object.

        table_name (str):
            Name of the table to create or manage.

        schema (dict[str, Type[Any]]):
            Dictionary mapping column names to Python types.

        qualifiers (dict[str, str] | None):
            Optional dictionary mapping column names to SQLite qualifiers (e.g., "NOT NULL").

        table_constraints (list[str] | None):
            Optional list of table-level constraints (e.g., "PRIMARY KEY").

        indices (list[list[str]] | None):
            Optional list of lists, where each inner list contains column names for an index.

        logger (Logger | None):
            Optional logger for logging messages. If None, no logging will be performed.
    """

    # List of allowed SQLite qualifiers
    ALLOWED_QUALIFIERS = (
        "NOT NULL",
        "NULL",
        "PRIMARY KEY",
        "UNIQUE",
        "CHECK",
        "DEFAULT",
        "COLLATE",
        "REFERENCES",
    )
    # Mapping of Python types to SQLite column types
    PYTHON_TO_SQLITE: dict[Type[Any], str] = {
        int: "INTEGER",
        float: "REAL",
        str: "TEXT",
        bytes: "BLOB",

        # Custom types
        bool: "BOOL",
        IPv4Address: "IPADDR",
        IPv6Address: "IPADDR",
        np.datetime64: "TIMESTAMP",
        pd.Timestamp: "TIMESTAMP",
    }
    # Teaches sqlite3 how to convert Python types to SQLite types
    SQLITE3_ADAPTERS: dict[Type[Any], Callable] = {
        # Boolean
        bool: lambda b: int(b),
        np.bool_: lambda b: int(b),

        # IP Addresses
        IPv4Address: lambda ip: ip.packed,
        IPv6Address: lambda ip: ip.packed,

        # Timestamps
        np.datetime64: lambda ts: pd.to_datetime(ts).isoformat(),
        pd.Timestamp: lambda ts: ts.isoformat(),

        # N/A types
        type(pd.NA): lambda na: None,
        type(pd.NaT): lambda nat: None,

        # Integers
        np.int64: lambda x: int(x),

        # Floats
        np.float64: lambda x: float(x),
    }
    # SQLite type -> Python type conversion is split between SQLite3 and pandas
    # (for ease and performance)
    SQLITE3_CONVERTERS: dict[str, Callable] = {
        # Leave IP addresses as is, we will handle it ourselves later
        "IPADDR": lambda ip: ip,
        # Just bytes -> str for timestamps (Pandas will handle the rest)
        "TIMESTAMP": lambda ts: ts.decode() if isinstance(ts, bytes) else ts,
    }
    PANDAS_DTYPE_MAP: dict[type, str] = {
        bool:   'boolean',
        int:    'Int64',
        float:  'Float64',
        str:    'string',
    }

    def __init__(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        schema: dict[str, Type[Any]],
        qualifiers: dict[str, str] | None = None,
        table_constraints: list[str] | None = None,
        indices: list[list[str]] | None = None,
        logger: Logger | None = None,
    ):
        self.conn = conn
        self.table_name = table_name
        self.schema = schema

        def _normalize(text: str):
            return " ".join(text.split()).lower().strip()

        self.qualifiers = qualifiers or {}
        self.qualifiers = {
            col_name: _normalize(qualifier_sql)
            for col_name, qualifier_sql in self.qualifiers.items()
        }

        self.table_constraints = table_constraints or []
        self.table_constraints = [
            _normalize(tc) for tc in self.table_constraints
        ]

        self.indices = indices or []
        self.logger = logger
        self._create_verify_table()

    def _create_verify_table(self):
        # Build column definitions
        cols_def: list[str] = []
        for col_name, py_type in self.schema.items():
            # Convert Python type to SQLite type
            sql_type = self.PYTHON_TO_SQLITE.get(py_type)
            if sql_type is None:
                raise TypeError(f"Unsupported type in schema: {py_type}")

            # Validate qualifier syntax by attempting to create a temp table
            qualifier_sql = self.qualifiers.get(col_name, "").strip()
            if qualifier_sql:
                uq = qualifier_sql.upper()
                if not any(uq.startswith(pref) for pref in self.ALLOWED_QUALIFIERS):
                    raise ValueError(
                        f"Invalid qualifier syntax for column '{col_name}': '{qualifier_sql}'"
                    )
            try:
                test_stmt = f"""
                    CREATE TEMP TABLE __test__ ({col_name} {sql_type} {qualifier_sql});
                """
                self.conn.execute(test_stmt)
                self.conn.execute("DROP TABLE IF EXISTS __test__;")
            except sqlite3.OperationalError as e:
                raise ValueError(
                    f"Invalid qualifier syntax for column '{col_name}': {qualifier_sql}, "
                    f"SQLite error: {e}"
                )
            col_def = f"{col_name} {sql_type} {qualifier_sql}".strip()
            cols_def.append(col_def)

        # Add any table-level constraints
        cols_def += self.table_constraints

        # Create table and indices
        try:
            with self.conn:
                ddl = f"CREATE TABLE IF NOT EXISTS {self.table_name} ({', '.join(cols_def)});"
                self.conn.execute(ddl)

                # Create indices
                for idx_cols in self.indices:
                    idx_name = f"idx_{self.table_name}_{'_'.join(idx_cols)}"
                    cols_list = ", ".join(idx_cols)
                    self.conn.execute(
                        f"CREATE INDEX IF NOT EXISTS {idx_name} ON {self.table_name} ({cols_list});"
                    )

            if self.logger:
                self.logger.info(
                    f"Successfully executed create table query: {ddl}"
                )
                self.logger.info(
                    f"Successfully created indices {self.indices} for table {self.table_name}"
                )
        except sqlite3.DatabaseError as e:
            raise RuntimeError(
                f"Error while creating table or indices for '{self.table_name}': {e}"
            ) from e

        # Validate existing schema: columns and types
        info = self.conn.execute(
            f"PRAGMA table_info({self.table_name});"
        ).fetchall()
        existing_cols = {row[1]: row[2].upper() for row in info}
        for col_name, py_type in self.schema.items():
            expected = self.PYTHON_TO_SQLITE[py_type]
            actual = existing_cols.get(col_name)
            if actual is None or not actual.startswith(expected):
                raise ValueError(
                    f"Column '{col_name}' type mismatch: expected '{expected}', got '{actual}'"
                )

        # Validate constraints and qualifiers
        # NOTE: This is a simple check and may not cover all cases
        ddl_info = self.conn.execute(
            f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?;",
            (self.table_name,),
        ).fetchone()
        if ddl_info:
            table_sql = ddl_info[0].upper()

            # Validate table-level constraints
            for tc in self.table_constraints:
                if tc.upper() not in table_sql:
                    raise ValueError(f"Missing table constraint in DDL: {tc}")

            # Validate column-level qualifiers
            for col, qualifier_sql in self.qualifiers.items():
                qual = qualifier_sql.strip().upper()
                if qual and qual not in table_sql:
                    raise ValueError(
                        f"Missing qualifier '{qualifier_sql}' for column '{col}' in DDL"
                    )

        # Validate indices
        idx_list = self.conn.execute(
            f"PRAGMA index_list({self.table_name});"
        ).fetchall()
        existing_idxs = {row[1] for row in idx_list}
        for idx_cols in self.indices:
            idx_name = f"idx_{self.table_name}_{'_'.join(idx_cols)}"
            if idx_name not in existing_idxs:
                raise ValueError(f"Missing index: {idx_name}")

        if self.logger:
            self.logger.info(
                f"Table '{self.table_name}' schema verified successfully."
            )
            self.logger.info(f"Table constraints: {self.table_constraints}")
            self.logger.info(f"Qualifiers: {self.qualifiers}")
            self.logger.info(f"Indices: {self.indices}")

    def cast_df_types_schema(self, df: pd.DataFrame):
        """
        Cast DataFrame columns to match the table schema types.

        Args:
            df (pd.DataFrame):
                DataFrame to cast.

        Returns:
            pd.DataFrame:
                DataFrame with columns cast to match the table schema types.
        """

        for col, typ in self.schema.items():
            if col not in df.columns:
                continue

            # PANDAS_DTYPE_MAP maps some common types
            if typ in self.PANDAS_DTYPE_MAP:
                df[col] = df[col].astype(self.PANDAS_DTYPE_MAP[typ])

            # Handle certain specific types
            elif typ is np.datetime64:
                df[col] = pd.to_datetime(df[col], errors="coerce")
            elif typ is IPv4Address or typ is IPv6Address:
                df[col] = df[col].map(
                    lambda x: try_convert_ip(x) if pd.notna(x) else pd.NA
                )
            elif typ is bytes:
                df[col] = df[col].map(
                    lambda x: x if pd.notna(x) else pd.NA
                )

            # Fallback: try a direct astype(typ), but ignore failures
            else:
                try:
                    df[col] = df[col].astype(typ)
                except Exception:
                    pass

        return df

    def insert_df(
        self,
        df: pd.DataFrame,
        if_exists: str = "abort",
        cast_columns_to_schema: bool = True,
    ):
        """
        Insert a pandas DataFrame into the table, converting types as needed.

        Args:
            df (pd.DataFrame):
                DataFrame to insert into the table.

            if_exists (str):
                What to do if the rows being inserted would cause a conflict.

                - `fail`: On conflict, raise an error but keep changes made so far.
                - `replace`: On conflict, replace the existing row with the new row.
                - `abort`: On conflict, raise an error and roll back all changes made so far.
                - `ignore`: On conflict, skip the new row.

            cast_columns_to_schema (bool):
                Whether to cast DataFrame columns to match the table schema types.
                Default is True, which will attempt to convert DataFrame types to match the schema.
        """
        # Map if_exists to SQLite conflict clauses
        if_exists_map = {
            "fail": "INSERT OR FAIL",
            "replace": "INSERT OR REPLACE",
            "abort": "INSERT",
            "ignore": "INSERT OR IGNORE",
        }
        if_exists = if_exists.lower()
        if if_exists not in if_exists_map:
            raise ValueError(
                f"if_exists='{if_exists}' not one of {', '.join(if_exists_map.keys())}"
            )
        prefix = if_exists_map[if_exists]

        # Build the INSERT statement
        cols = list(self.schema.keys())
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        stmt = (
            f"{prefix} INTO {self.table_name} "
            f"({col_list}) VALUES ({placeholders})"
        )

        # Reorder DataFrame columns to match schema
        df = df.reindex(columns=cols)

        # Cast DataFrame types to match schema if needed
        if cast_columns_to_schema:
            df = self.cast_df_types_schema(df)

        # Use generator to reduce memory footprint
        def _gen_rows():
            for row in df.itertuples(index=False, name=None):
                yield row

        # Insert with conversion context
        with _sqlite3_conversion_ctx():
            with self.conn:
                self.conn.executemany(stmt, _gen_rows())

        if self.logger:
            self.logger.info(
                f"Inserted {len(df)} rows into table '{self.table_name}'."
            )

    def query_all(self):
        """
        Query every row/column in the table and return a DataFrame.
        """
        cols = ", ".join(self.schema.keys())
        sql = f"SELECT {cols} FROM {self.table_name};"
        return self.query(sql)

    def query(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> pd.DataFrame:
        """
        Execute a raw SQL query and return results as a DataFrame.

        Args:
            sql (str):
                The SQL query to execute.

            params:
                Parameters to pass to the SQL query.

        Returns:
            df (pd.DataFrame):
                A single DataFrame with the results.
        """
        # We need to somehow identify the requested columns.
        # Here, we briefly execute the query and then close the cursor to get the result schema.
        with _sqlite3_conversion_ctx():
            cur = self.conn.execute(sql, params)
            query_cols = [desc[0] for desc in cur.description]
            cur.close()

        # Identify columns which need to be parsed as dates
        parse_dates = [
            col_name for col_name, py_type in self.schema.items()
            if col_name in query_cols and self.PYTHON_TO_SQLITE[py_type] == "TIMESTAMP"
        ]

        # Identify columns which need to be converted to pandas dtypes
        dtype = {
            col_name: self.PANDAS_DTYPE_MAP[py_type]
            for col_name, py_type in self.schema.items()
            if col_name in query_cols and py_type in self.PANDAS_DTYPE_MAP
        }

        with _sqlite3_conversion_ctx():
            df = pd.read_sql_query(
                sql,
                self.conn,
                params=params,
                parse_dates=parse_dates,
                dtype=dtype,
            )

        # Post-process IP addresses if needed
        if ip_cols := [
            col_name for col_name, py_type in self.schema.items()
            if col_name in query_cols and self.PYTHON_TO_SQLITE[py_type] == "IPADDR"
        ]:
            raw_ips = pd.unique(
                pd.concat([df[col] for col in ip_cols], ignore_index=True)
            )

            # Build a mapping from raw IPs to converted IPAddress objects (or leave as is)
            mapping = {}
            for raw_ip in raw_ips:
                mapping[raw_ip] = try_convert_ip(raw_ip)

            # Apply the mapping to each IP column
            for col in ip_cols:
                df[col] = df[col].map(mapping)

        if self.logger:
            self.logger.info(
                f"Executed query: {sql} with params: {params}, returned {len(df)} rows."
            )

        return df


class SqliteDatabase:
    """
    A class to manage a SQLite database connection.
    This class allows registering tables with a defined schema, including qualifiers, constraints, and indices.

    Args:
        db_path (str | Path):
            Path to the SQLite database file. If the file does not exist, it will be created.

        wal (bool):
            Whether to use Write-Ahead Logging mode for the database. Default is True.

        uri (bool):
            If True, `db_path` is treated as a URI. Default is False.

        **connect_kwargs:
            Additional keyword arguments to pass to `sqlite3.connect()`.
    """

    CACHE_KB_MIN = 2_000
    CACHE_KB_DEFAULT = 256_000

    def __init__(
        self,
        db_path: str | Path,
        wal: bool = True,
        uri: bool = False,
        cache_kb: int = CACHE_KB_DEFAULT,
        logger: Logger | None = None,
        **connect_kwargs
    ):
        self.path = Path(db_path).resolve()

        self.conn = self._connect_with_conversion(
            self.path, uri=uri, **connect_kwargs
        )
        if cache_kb > self.CACHE_KB_MIN:
            self.conn.execute(f"PRAGMA cache_size=-{cache_kb};")
        if wal:
            self.conn.execute("PRAGMA journal_mode=WAL;")

        self.tables: dict[str, SqliteTable] = {}

        self.logger = logger
        if self.logger:
            self.logger.info(f"Connected to SQLite database at {db_path}")

    def _connect_with_conversion(self, db_path: str, **kwargs):
        with _sqlite3_conversion_ctx():
            return sqlite3.connect(
                db_path,
                detect_types=sqlite3.PARSE_DECLTYPES,
                **kwargs
            )

    def register_table(
        self,
        table_name: str,
        schema: dict[str, Type[Any]],
        qualifiers: dict[str, str] | None = None,
        table_constraints: list[str] | None = None,
        indices: list[list[str]] | None = None
    ):
        """
        Register a table with the database, creating it if it does not already exist.

        Args:
            table_name (str):
                Name of the table to register.

            schema (dict[str, Type[Any]]):
                Dictionary mapping column names to Python types.

            qualifiers (dict[str, str] | None):
                Optional dictionary mapping column names to SQLite qualifiers (e.g., "NOT NULL").

            table_constraints (list[str] | None):
                Optional list of table-level constraints (e.g., "PRIMARY KEY").

            indices (list[list[str]] | None):
                Optional list of lists, where each inner list contains column names for an index.

        Returns:
            table (SqliteTable):
                An instance of `SqliteTable` representing the registered table.
        """

        if table_name in self.tables:
            return self.tables[table_name]
        table = SqliteTable(
            self.conn,
            table_name,
            schema,
            qualifiers=qualifiers,
            table_constraints=table_constraints,
            indices=indices,
            logger=self.logger,
        )
        self.tables[table_name] = table

        if self.logger:
            self.logger.info(
                f"Registered table '{table_name}' with schema: {schema}"
            )

        return table

    def close(self):
        """
        Close the database connection.
        """

        if self.conn:
            self.conn.close()
            self.conn = None
            if self.logger:
                self.logger.info(
                    f"Closed SQLite database connection at {self.path}"
                )
