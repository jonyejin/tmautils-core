# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from functools import lru_cache
from pathlib import Path
import gzip
import duckdb
import pandas as pd
import pyarrow as pa
import requests

from tmautils.common import IOHelper, atomic_write, path_temp_suffix


class CaidaAsOrgInfoUtil:
    """
    Utility class for interacting with the CAIDA AS-Organization dataset.

    Args:
        date_str (str):
            Date string in the ISO format (YYYY-MM-DD)

        force_refresh (bool):
            If True, re-download and reprocess data even if cached files exist.
            Default is False.

        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    def __init__(
        self,
        date_str: str,
        *,
        force_refresh: bool = False,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        **kwargs,
    ):
        working_root = IOHelper.handle_working_root_data_dir(
            working_root, data_dir
        )
        self.io_helper = IOHelper.init_with_dirs(
            self.__class__.__name__,
            dirs={"raw", "processed", "logs"},
            working_root=working_root,
            **kwargs,
        )

        # Convert date_str from YYYY-MM-DD to YYYYMMDD for URL
        self.date_str = date_str.replace('-', '')

        # File paths
        self._gz_path = (
            self.io_helper.raw / f"{date_str}.as-org2info.v0.txt.gz"
        )
        self._parquet_path = (
            self.io_helper.processed / f"{date_str}.as-org2info.v0.parquet"
        )

        # Lazy-loaded DataFrames for backward compatibility
        self._df_org_id: pd.DataFrame | None = None
        self._df_aut: pd.DataFrame | None = None

        # Handle force refresh
        if force_refresh:
            self._cleanup_cached_files()

        # Check if we need to download/process
        if not self._parquet_path.exists() or not self._validate_parquet():
            self._download_and_process()
        else:
            self.io_helper.logger.info(
                f"Loaded {self._parquet_path.name} from disk."
            )

    def _cleanup_cached_files(self) -> None:
        self._parquet_path.unlink(missing_ok=True)
        self._gz_path.unlink(missing_ok=True)
        self.io_helper.logger.info("Cleaned cached files")

    def _validate_parquet(self) -> bool:
        try:
            result = duckdb.query(f"""
                SELECT COUNT(*) as cnt
                FROM read_parquet('{self._parquet_path}')
                LIMIT 1
            """).fetchone()
            return result is not None and result[0] > 0
        except Exception as e:
            self.io_helper.logger.warning(
                f"Parquet validation failed: {e}. Will reprocess."
            )
            self._parquet_path.unlink(missing_ok=True)
            return False

    def _download_and_process(self) -> None:
        # Check if we need to download
        if not self._gz_path.exists():
            self._download_gz()

        # Try to parse and save
        try:
            self._parse_and_save()
        except Exception as e:
            self.io_helper.logger.error(
                f"Failed to parse {self._gz_path.name}: {e}. Attempting re-download..."
            )
            # Delete corrupt gz and try again
            self._gz_path.unlink(missing_ok=True)
            self._download_gz()
            self._parse_and_save()  # Let it raise if still fails

    def _download_gz(self) -> None:
        self.io_helper.logger.info("Downloading AS2Org data from CAIDA...")

        url = (
            f"https://publicdata.caida.org/datasets/as-organizations/versions/"
            f"{self.date_str}.as-org2info.v0.txt.gz"
        )

        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with atomic_write(self._gz_path) as f:
            f.write(resp.content)
        self.io_helper.logger.info(f"Downloaded {self._gz_path.name}")

    def _parse_and_save(self) -> None:
        org_id_rows: list[dict[str, str]] = []
        aut_rows: list[dict[str, str]] = []
        current_table: str | None = None
        current_cols: list[str] | None = None

        with gzip.open(self._gz_path, 'rt') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                if line.startswith('# format:'):
                    format_str = line.split(':', 1)[1].strip()
                    current_cols = format_str.split('|')
                    # Determine table by first column
                    current_table = 'org_id' if current_cols[0] == 'org_id' else 'aut'
                elif line.startswith('#'):
                    continue
                else:
                    if current_cols:
                        fields = line.split('|')
                        row = dict(zip(current_cols, fields))
                        if current_table == 'org_id':
                            org_id_rows.append(row)
                        else:
                            aut_rows.append(row)

        if not org_id_rows or not aut_rows:
            raise ValueError(
                f"Expected both org_id and aut tables, "
                f"but parsing failed for {self._gz_path.name}"
            )

        self.io_helper.logger.info(
            f"Parsed {len(aut_rows):,} ASNs and {len(org_id_rows):,} orgs"
        )

        # Convert to Arrow tables to register in DuckDB
        org_id_table = pa.Table.from_pylist(org_id_rows)
        aut_table = pa.Table.from_pylist(aut_rows)

        tmp_path = path_temp_suffix(self._parquet_path)
        con = duckdb.connect()
        try:
            con.register('org_id_data', org_id_table)
            con.register('aut_data', aut_table)
            con.execute(f"""
                COPY (
                    SELECT
                        CAST(a.aut AS INTEGER) AS aut,
                        TRY_STRPTIME(a.changed, '%Y%m%d')::DATE AS changed,
                        a.aut_name,
                        a.org_id,
                        a.opaque_id,
                        a.source,
                        o.org_name,
                        o.country
                    FROM aut_data a
                    LEFT JOIN org_id_data o ON a.org_id = o.org_id
                ) TO '{tmp_path}' (FORMAT PARQUET)
            """)
            tmp_path.rename(self._parquet_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            con.close()

        self.io_helper.logger.info(f"Wrote {self._parquet_path.name} to disk.")

    @lru_cache(maxsize=10_000)
    def _cached_lookup(self, asn: int) -> tuple[str | None, str | None]:
        result = duckdb.query(f"""
            SELECT aut_name, org_name
            FROM read_parquet('{self._parquet_path}')
            WHERE aut = {asn}
            LIMIT 1
        """).fetchone()

        if result is None:
            return None, None
        return result[0], result[1]

    def lookup(
        self,
        asn: int,
    ) -> tuple[str | None, str | None]:
        """
        Lookup the organization name and autonomous system name for a given ASN.

        Args:
            asn (int):
                ASN to query.

        Returns:
            (aut_name, org_name):
                Tuple containing the autonomous system name and organization name.
                One or both may be None if not found.
        """
        return self._cached_lookup(asn)

    def clear_cache(self) -> None:
        """Clear the LRU cache for lookup results."""
        self._cached_lookup.cache_clear()

    def annotate_df(
        self,
        df: pd.DataFrame,
        asn_col: str = 'asn',
        as_name_col: str = 'as_name',
        org_name_col: str = 'org_name',
    ) -> pd.DataFrame:
        """
        Annotate a DataFrame with autonomous system and organization names.

        Args:
            df (pandas.DataFrame):
                DataFrame to annotate.

            asn_col (str):
                Column name containing ASN values.

            as_name_col (str):
                Column name to store autonomous system names.

            org_name_col (str):
                Column name to store organization names.

        Returns:
            pandas.DataFrame:
                Annotated DataFrame with new columns for autonomous system and organization names.
        """
        con = duckdb.connect()
        try:
            con.register('input_df', df)
            result = con.execute(f"""
                SELECT d.*, j.aut_name AS {as_name_col}, j.org_name AS {org_name_col}
                FROM input_df d
                LEFT JOIN read_parquet('{self._parquet_path}') j
                    ON CAST(d.{asn_col} AS INTEGER) = j.aut
            """).df()
        finally:
            con.close()
        return result

    @property
    def df_aut(self) -> pd.DataFrame:
        """Backward-compatible property returning `aut` DataFrame."""
        if self._df_aut is None:
            self._df_aut = duckdb.query(f"""
                SELECT aut, changed, aut_name, org_id, opaque_id, source
                FROM read_parquet('{self._parquet_path}')
            """).df()
            self._df_aut.set_index('aut', inplace=True)
        return self._df_aut

    @property
    def df_org_id(self) -> pd.DataFrame:
        """Backward-compatible property returning `org_id` DataFrame."""
        if self._df_org_id is None:
            self._df_org_id = duckdb.query(f"""
                SELECT DISTINCT org_id, org_name, country
                FROM read_parquet('{self._parquet_path}')
                WHERE org_id IS NOT NULL
            """).df()
            self._df_org_id.set_index('org_id', inplace=True)
        return self._df_org_id
