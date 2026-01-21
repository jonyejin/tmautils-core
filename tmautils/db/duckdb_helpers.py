# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass
import duckdb
from pytricia import PyTricia
from ipaddress import ip_network

from tmautils.common import IPAddress

@dataclass
class DuckDbInetLpmIndex:
    """
    Longest-Prefix-Match (LPM) helper for DuckDB inet-like data.
    This class builds LPM indexes using PyTricia tries
    and provides lookup functionality both in Python and as a DuckDB UDF.

    NOTE: Use `DuckDbInetLpmIndex.from_relation` to create an instance of this class.

    Example usage:
    ```
        rel = con.execute(\"""
            SELECT
                network::INET::VARCHAR AS network,
                is_proxy,
                proxy_type
            FROM read_parquet('data.parquet')
        \""")

        idx = DuckDbInetLpmIndex.from_relation(
            rel,
            network_col="network",
            value_cols=["is_proxy", "proxy_type"],
        )
    ```
    """

    trie4: PyTricia
    trie6: PyTricia
    value_cols: tuple[str, ...]

    @classmethod
    def from_relation(
        cls,
        rel: duckdb.DuckDBPyRelation,
        network_col: str = "network",
        value_cols: Tuple[str, ...] = (),
    ) -> "DuckDbInetLpmIndex":
        """
        Build an LPM index from a DuckDB relation.
        Before calling this, you must ensure that the relation contains
        the network column as `prefix/mask` strings
        (the `/mask` suffix can be omitted for `/32` IPv4 and `/128` IPv6 addresses).
        Example correct strings: `127.0.0.1`, `192.168.1.0/24`, `2001:db8::/32`, `::1`.
        One of the ways to do this is by casting a DuckDB INET column to VARCHAR.

        Args:
            rel (duckdb.DuckDBPyRelation):
                DuckDB relation containing network and value columns.

            network_col (str):
                Name of the column containing network prefixes.

            value_cols (Tuple[str, ...]):
                Names of the columns containing values to associate with
                each network prefix.

        Returns:
            DuckDbInetLpmIndex:
                An instance of the LPM index.
        """
        trie4 = PyTricia(32)
        trie6 = PyTricia(128)

        # Stream Arrow record batches from DuckDB
        for batch in rel.fetch_record_batch():
            networks = batch[network_col].to_pylist()
            value_lists = [batch[c].to_pylist() for c in value_cols]

            for idx_row, network in enumerate(networks):
                if network is None:
                    raise ValueError(
                        f"Network column '{network_col}' contains NULL value "
                        f"at row {idx_row}."
                    )

                net = ip_network(network, strict=False)
                trie = trie4 if net.version == 4 else trie6
                values = tuple(v[idx_row] for v in value_lists)
                trie[str(net)] = values

        return cls(trie4=trie4, trie6=trie6, value_cols=value_cols)

    def lookup(self, ip: IPAddress | str | None) -> Optional[Tuple[Any, ...]]:
        """
        Lookup an IP address and return the longest-prefix-match values.

        Args:
            ip (IPv4Address | IPv6Address | str | None):
                IP address to lookup.

        Returns:
            A tuple of values corresponding to the longest-prefix-match.
            If no match is found, returns None.
        """
        if ip is None:
            return None
        ip_str = str(ip) if not isinstance(ip, str) else ip
        trie = self.trie6 if ":" in ip_str else self.trie4
        return trie.get(ip_str)

    def lookup_dict(self, ip: IPAddress | str | None) -> Dict[str, Any | None]:
        """
        Lookup an IP address and return a dictionary mapping value column names
        to their corresponding longest-prefix-match values.

        Args:
            ip (IPv4Address | IPv6Address | str | None):
                IP address to lookup.

        Returns:
            A dictionary mapping each value column name to its corresponding
            value for the longest-prefix-match.
            If no match is found, all values will be None.
        """
        res = self.lookup(ip)
        if res is None:
            return {name: None for name in self.value_cols}
        return {name: val for name, val in zip(self.value_cols, res)}

    def register_struct_udf(
        self,
        con: duckdb.DuckDBPyConnection,
        func_name: str = "lpm_lookup",
        field_types: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Register a struct-returning UDF in DuckDB using this index.

        Args:
            con (DuckDBPyConnection):
                Connection in which to register the function.

            func_name (str):
                Name of the UDF in SQL.

            field_types (Optional[Dict[str, str]]):
                Mapping from field name to DuckDB type string.
                If omitted, defaults to VARCHAR for all fields.

                Example:
                ```
                field_types = {
                    "is_proxy": "BOOLEAN",
                    "proxy_type": "VARCHAR",
                }
                ```
        """
        def _udf(ip: str):
            return self.lookup_dict(ip)

        if field_types is None:
            field_types = {}
        field_specs = [
            f"{name} {field_types.get(name, 'VARCHAR')}"
            for name in self.value_cols
        ]

        struct_type = "STRUCT(" + ", ".join(field_specs) + ")"

        con.create_function(
            func_name,
            _udf,
            parameters=[str],
            return_type=struct_type,
        )
