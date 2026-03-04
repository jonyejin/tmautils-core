# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu, Yejin Cho

from functools import lru_cache
from pathlib import Path
import csv
import re
from typing import Optional
import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import requests

from tmautils.common import IOHelper, atomic_write, path_temp_suffix


class ASdbCategoryUtil:
    """
    Utility class for interacting with the ASdb dataset.

    Args:
        year (int):
            Year of the ASdb dataset to download.
            Default is 2024.

        month (int):
            Month of the ASdb dataset to download.
            Default is 1.

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
        year: int = 2024,
        month: int = 1,
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

        self._year = year
        self._month = month

        # File paths
        self._data_csv_path = (
            self.io_helper.raw / f"{year}-{month:02d}_categorized_ases.csv"
        )
        self._category_csv_path = self.io_helper.raw / "NAICSlite.csv"
        self._parquet_path = (
            self.io_helper.processed / f"{year}-{month:02d}_asdb.parquet"
        )

        # URLs for downloading
        self._data_url = (
            f"https://asdb.stanford.edu/data/{year}-{month:02d}_categorized_ases.csv"
        )
        self._category_url = "https://asdb.stanford.edu/data/NAICSlite.csv"

        # Lazy-loaded DataFrame for backward compatibility
        self._df: pd.DataFrame | None = None

        # Category dict
        self.category: dict[str, list[str]] | None = None

        # Handle force refresh
        if force_refresh:
            self._cleanup_cached_files()

        # Check if we need to download/process
        if not self._parquet_path.exists() or not self._validate_parquet():
            self._download_and_process()
        else:
            self._build_category_dict()
            self.io_helper.logger.info(
                f"Loaded {self._parquet_path.name} from disk."
            )

    def _cleanup_cached_files(self) -> None:
        self._parquet_path.unlink(missing_ok=True)
        self.io_helper.logger.info("Cleaned cached parquet file")

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
        # Download CSV files if needed
        if not self._data_csv_path.exists():
            self._download_file(self._data_url, self._data_csv_path)
        if not self._category_csv_path.exists():
            self._download_file(self._category_url, self._category_csv_path)

        # Build category dict
        self._build_category_dict()

        # Process and save parquet
        try:
            self._parse_and_save()
        except Exception as e:
            self.io_helper.logger.error(
                f"Failed to parse CSV: {e}. Attempting re-download..."
            )
            self._data_csv_path.unlink(missing_ok=True)
            self._download_file(self._data_url, self._data_csv_path)
            self._parse_and_save()  # Let it raise if still fails

    def _download_file(self, url: str, dest: Path) -> None:
        self.io_helper.logger.info(f"Downloading {url}...")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with atomic_write(dest) as f:
            f.write(resp.content)
        self.io_helper.logger.info(f"Downloaded {dest.name}")

    def _build_category_dict(self) -> None:
        category: dict[str, list[str | None]] = {}
        try:
            with open(self._category_csv_path, 'r', newline='\n') as file:
                category_reader = csv.reader(file, delimiter=',')
                current_category: str | None = None
                for index, row in enumerate(category_reader):
                    if index == 0:
                        continue  # Skip header row
                    else:
                        name, level = row[:2]
                        if level == "1":
                            category[name] = []
                            current_category = name
                        elif level == "2":
                            if current_category is None or name == "":
                                continue  # Skip invalid entries
                            category[current_category].append(name)
            self.category = category
            self.io_helper.logger.info(
                f"Loaded ASdb category dataset from {self._category_csv_path.name}"
            )
        except FileNotFoundError:
            self.io_helper.logger.error(
                f"Category file not found: {self._category_csv_path}"
            )
            raise
        except KeyError:
            self.io_helper.logger.error(
                f"Invalid category format in file: {self._category_csv_path}"
            )
            raise
        except Exception as e:
            self.io_helper.logger.error(f"Error reading category file: {e}")
            raise

    def _parse_and_save(self) -> None:
        self.io_helper.logger.info(f"Parsing {self._data_csv_path.name}...")

        # Read CSV with DuckDB into Arrow table
        # Use null_padding=true because ASdb CSVs have variable-width rows
        con = duckdb.connect()
        try:
            raw = con.execute(f"""
                SELECT * FROM read_csv('{self._data_csv_path}',
                    header=true, null_padding=true)
            """).fetch_arrow_table()
        finally:
            con.close()

        # Identify Layer 1/2 columns and their category indices
        l1_cols: dict[int, str] = {}  # {cat_idx: col_name}
        l2_cols: dict[int, str] = {}
        for col_name in raw.schema.names:
            match = re.search(r'Category (\d+)', col_name)
            if match:
                cat_idx = int(match.group(1))
                if "Layer 1" in col_name:
                    l1_cols[cat_idx] = col_name
                elif "Layer 2" in col_name:
                    l2_cols[cat_idx] = col_name

        # Prepare ASN array (strip "AS" prefix, convert to int)
        asn_col = raw.column("ASN")
        asn_stripped = pc.utf8_ltrim(asn_col, characters="AS")
        asn_ints = pc.cast(asn_stripped, pa.int64())

        # Build output arrays
        output_chunks: dict[str, list[pa.Array]] = {
            "asn": [], "cat_idx": [], "layer1": [], "layer2": []
        }

        for cat_idx in sorted(l1_cols.keys()):
            l1_array = raw.column(l1_cols[cat_idx])
            l2_array = raw.column(
                l2_cols[cat_idx]
            ) if cat_idx in l2_cols else None

            # Filter mask: l1 is not null and not empty
            l1_filled = pc.fill_null(l1_array, "")
            l1_trimmed = pc.utf8_trim_whitespace(l1_filled)
            mask = pc.not_equal(l1_trimmed, "")

            # Filter arrays by mask
            filtered_asn = pc.filter(asn_ints, mask)
            filtered_l1 = pc.filter(l1_array, mask)

            # Handle l2: filter and convert empty strings to null
            if l2_array is not None:
                filtered_l2 = pc.filter(l2_array, mask)
                l2_filled = pc.fill_null(filtered_l2, "")
                l2_trimmed = pc.utf8_trim_whitespace(l2_filled)
                filtered_l2 = pc.if_else(
                    pc.equal(l2_trimmed, ""),
                    pa.scalar(None, type=pa.string()),
                    filtered_l2
                )
            else:
                filtered_l2 = pa.nulls(len(filtered_asn), type=pa.string())

            # Constant cat_idx array
            filtered_cat_idx = pa.array(
                [cat_idx] * len(filtered_asn), type=pa.int32()
            )

            output_chunks["asn"].append(filtered_asn)
            output_chunks["cat_idx"].append(filtered_cat_idx)
            output_chunks["layer1"].append(filtered_l1)
            output_chunks["layer2"].append(filtered_l2)

        # Concatenate all chunks into final table
        table = pa.table({
            "asn": pa.chunked_array(output_chunks["asn"]),
            "cat_idx": pa.chunked_array(output_chunks["cat_idx"]),
            "layer1": pa.chunked_array(output_chunks["layer1"]),
            "layer2": pa.chunked_array(output_chunks["layer2"]),
        })

        self.io_helper.logger.info(
            f"Parsed {table.num_rows:,} category assignments"
        )

        # Write parquet with DuckDB
        tmp_path = path_temp_suffix(self._parquet_path)
        con = duckdb.connect()
        try:
            con.register('data', table)
            con.execute(f"""
                COPY (SELECT * FROM data ORDER BY asn, cat_idx)
                TO '{tmp_path}' (FORMAT PARQUET)
            """)
            tmp_path.rename(self._parquet_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            con.close()

        self.io_helper.logger.info(f"Wrote {self._parquet_path.name} to disk.")

    @lru_cache(maxsize=10_000)
    def _cached_get_full(self, asn: int) -> tuple[tuple, ...]:
        result = duckdb.query(f"""
            SELECT asn, cat_idx, layer1, layer2
            FROM read_parquet('{self._parquet_path}')
            WHERE asn = {asn}
            ORDER BY cat_idx
        """).fetchall()
        return tuple(result)

    def get_full(self, asn: int) -> pd.DataFrame:
        """
        Get all categories for an ASN, ordered by priority (cat_idx).

        Args:
            asn (int):
                ASN to query.

        Returns:
            asinfo (pd.DataFrame):
                DataFrame containing the ASdb category information for the given ASN.
                If the ASN is not found, an empty DataFrame is returned.

                Example:
                ```
                   asn  cat_idx                    layer1                      layer2
                0  15169       1        Computer and...          Cloud Provider
                1  15169       3        Infrastructure         Content Delivery...
                ```
        """
        rows = self._cached_get_full(asn)
        if not rows:
            return pd.DataFrame(columns=['asn', 'cat_idx', 'layer1', 'layer2'])
        return pd.DataFrame(rows, columns=['asn', 'cat_idx', 'layer1', 'layer2'])

    def get(
        self,
        asn: int,
        layer1: str | None = None,
        layer2: str | None = None,
    ) -> pd.DataFrame:
        """
        Get the ASdb category for a given ASN, with optional filtering.

        Args:
            asn (int):
                ASN to query.
            layer1 (str | None):
                Layer 1 category to filter by.
            layer2 (str | None):
                Layer 2 category to filter by.

        Returns:
            pd.DataFrame:
                DataFrame containing the ASdb category information for the given ASN.
                If the ASN is not found, an empty DataFrame is returned.
        """
        asninfo = self.get_full(asn)
        if layer1 is not None:
            asninfo = asninfo[asninfo["layer1"] == layer1]
        if layer2 is not None:
            asninfo = asninfo[asninfo["layer2"] == layer2]
        return asninfo.reset_index(drop=True)

    def find_ases_in_category(
        self,
        layer1_category: str,
        layer2_category: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Return all ASNs matching the given primary and (optional) secondary category.

        Args:
            layer1_category (str): Name of the Layer-1 category to match.
            layer2_category (Optional[str]): Name of the Layer-2 category to match (if any).

        Returns:
            df (pd.DataFrame):
                DataFrame containing the ASNs that match the given categories.
                If no ASNs match, an empty DataFrame is returned.
        """
        # Escape single quotes in category names for SQL
        l1_escaped = layer1_category.replace("'", "''")

        if layer2_category is not None:
            l2_escaped = layer2_category.replace("'", "''")
            query = f"""
                SELECT DISTINCT asn, cat_idx, layer1, layer2
                FROM read_parquet('{self._parquet_path}')
                WHERE layer1 = '{l1_escaped}' AND layer2 = '{l2_escaped}'
                ORDER BY asn, cat_idx
            """
        else:
            query = f"""
                SELECT DISTINCT asn, cat_idx, layer1, layer2
                FROM read_parquet('{self._parquet_path}')
                WHERE layer1 = '{l1_escaped}'
                ORDER BY asn, cat_idx
            """
        return duckdb.query(query).df()

    def clear_cache(self) -> None:
        """Clear the LRU cache for lookup results."""
        self._cached_get_full.cache_clear()

    @property
    def df(self) -> pd.DataFrame:
        """Backward-compatible property returning full DataFrame."""
        if self._df is None:
            self._df = duckdb.query(f"""
                SELECT asn, cat_idx, layer1, layer2
                FROM read_parquet('{self._parquet_path}')
                ORDER BY asn, cat_idx
            """).df()
        return self._df
