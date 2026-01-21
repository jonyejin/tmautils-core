# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Optional
from pathlib import Path
import duckdb
from dataclasses import dataclass
from uuid import uuid7
import contextlib

import pyarrow as pa

from .base import ArrowBackend, TableConfig


@dataclass
class ParquetBackend(ArrowBackend):
    """
    Backend that writes each flush as a separate Parquet file using DuckDB.

    Parquet files are written under `base_path / <table_name> / <uuid7>.parquet`.

    Args:
        base_path:
            Base directory where Parquet files will be stored.

        duckdb_conn:
            Optional DuckDB connection to use.
            If not provided, a new connection will be created with default settings.
    """

    base_path: Path
    duckdb_conn: Optional[duckdb.DuckDBPyConnection] = None

    def __post_init__(self):
        if self.duckdb_conn is None:
            self.duckdb_conn = duckdb.connect()

    def flush_arrow(
        self,
        table_name: str,
        config: TableConfig,
        data: pa.Table,
    ) -> None:
        # Prepare directory
        table_dir = self.base_path / table_name
        table_dir.mkdir(parents=True, exist_ok=True)

        # Generate uuid7-based filename and table name
        uniq = uuid7()
        path = table_dir / f"{uniq}.parquet"
        tname = f"t_{uniq.hex}"

        cur = self.duckdb_conn.cursor()
        try:
            cur.register(tname, data)
            cur.execute(
                f"COPY {tname} TO '{str(path)}' (FORMAT 'PARQUET')"
            )
        finally:
            with contextlib.suppress(Exception):
                cur.unregister(tname)
            with contextlib.suppress(Exception):
                cur.close()
