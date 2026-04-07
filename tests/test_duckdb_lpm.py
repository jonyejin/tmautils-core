from __future__ import annotations

import os
from pathlib import Path

import pytest
import duckdb

from tmautils.db import DuckDbInetLpmIndex


def test_duckdb_inet_lpm_index_with_inet_to_varchar_cast():
    # 1) Make a tiny in-memory DuckDB and an example prefix table
    con = duckdb.connect(database=":memory:")

    con.execute("""
        CREATE TABLE ipinfo (
            network    INET,
            is_proxy   BOOLEAN,
            proxy_type VARCHAR
        );
    """)

    # Some toy prefixes
    con.execute("""
        INSERT INTO ipinfo (network, is_proxy, proxy_type) VALUES
            ('1.1.1.0/24',    TRUE,  'vpn'),
            ('1.1.1.1/32',    TRUE,  'tor'),         -- more specific than /24
            ('2.2.0.0/16',    TRUE,  'corp-proxy'),
            ('3.3.3.3',       FALSE, NULL),         -- single host, no proxy
            ('2001:db8::/32', TRUE,  'ipv6-proxy');
    """)

    # 2) Build a DuckDB relation and construct the LPM index
    rel = con.execute("""
        SELECT
            network::VARCHAR AS network,      -- explicit INET -> VARCHAR cast
            is_proxy,
            proxy_type
        FROM ipinfo
    """)

    idx = DuckDbInetLpmIndex.from_relation(
        rel,
        network_col="network",
        value_cols=("is_proxy", "proxy_type"),
    )

    # 3) Python-side lookups
    lookups = {
        ip: idx.lookup_dict(ip)
        for ip in [
            "1.1.1.1",
            "1.1.1.42",
            "2.2.3.4",
            "3.3.3.3",
            "9.9.9.9",
            "2001:db8::1",
        ]
    }

    # 1.1.1.1 -> /32 (tor)
    assert lookups["1.1.1.1"]["is_proxy"] is True
    assert lookups["1.1.1.1"]["proxy_type"] == "tor"

    # 1.1.1.42 -> /24 (vpn)
    assert lookups["1.1.1.42"]["is_proxy"] is True
    assert lookups["1.1.1.42"]["proxy_type"] == "vpn"

    # 2.2.3.4 -> 2.2.0.0/16 (corp-proxy)
    assert lookups["2.2.3.4"]["is_proxy"] is True
    assert lookups["2.2.3.4"]["proxy_type"] == "corp-proxy"

    # 3.3.3.3 -> exact host, no proxy
    assert lookups["3.3.3.3"]["is_proxy"] is False
    assert lookups["3.3.3.3"]["proxy_type"] is None

    # 9.9.9.9 -> no match
    assert lookups["9.9.9.9"]["is_proxy"] is None
    assert lookups["9.9.9.9"]["proxy_type"] is None

    # 2001:db8::1 -> ipv6 /32
    assert lookups["2001:db8::1"]["is_proxy"] is True
    assert lookups["2001:db8::1"]["proxy_type"] == "ipv6-proxy"

    # 4) Register a struct-returning UDF in DuckDB
    idx.register_struct_udf(
        con,
        func_name="lpm_lookup",
        field_types={
            "is_proxy": "BOOLEAN",
            "proxy_type": "VARCHAR",
        },
    )

    # 5) Use the UDF from SQL
    df = con.execute("""
        SELECT
            ip,
            (lpm_lookup(ip)).is_proxy    AS is_proxy,
            (lpm_lookup(ip)).proxy_type  AS proxy_type
        FROM (
            SELECT '1.1.1.1'      AS ip UNION ALL
            SELECT '1.1.1.42'     AS ip UNION ALL
            SELECT '2.2.3.4'      AS ip UNION ALL
            SELECT '3.3.3.3'      AS ip UNION ALL
            SELECT '9.9.9.9'      AS ip UNION ALL
            SELECT '2001:db8::1'  AS ip
        ) t
        ORDER BY ip;
    """).fetchall()

    # Check SQL results line up with Python lookups
    result_map = {}
    for ip, is_proxy, proxy_type in df:
        result_map[ip] = (is_proxy, proxy_type)

    assert result_map["1.1.1.1"] == (True, "tor")
    assert result_map["1.1.1.42"] == (True, "vpn")
    assert result_map["2.2.3.4"] == (True, "corp-proxy")
    assert result_map["3.3.3.3"] == (False, None)
    assert result_map["9.9.9.9"] == (None, None)
    assert result_map["2001:db8::1"] == (True, "ipv6-proxy")


@pytest.fixture
def sample_index():
    """Build a small LPM index for save/load tests."""
    con = duckdb.connect(database=":memory:")
    con.execute("""
        CREATE TABLE nets (network INET, is_proxy BOOLEAN, proxy_type VARCHAR);
        INSERT INTO nets VALUES
            ('1.1.1.0/24',    TRUE,  'vpn'),
            ('1.1.1.1/32',    TRUE,  'tor'),
            ('2001:db8::/32', TRUE,  'ipv6-proxy');
    """)
    rel = con.execute("""
        SELECT network::VARCHAR AS network, is_proxy, proxy_type FROM nets
    """)
    return DuckDbInetLpmIndex.from_relation(
        rel, network_col="network", value_cols=("is_proxy", "proxy_type"),
    )


def test_save_and_load_round_trip(sample_index, tmp_path):
    sample_index.save(tmp_path)

    loaded = DuckDbInetLpmIndex.load(
        tmp_path, value_cols=("is_proxy", "proxy_type"),
    )

    assert loaded.lookup("1.1.1.1") == (True, "tor")
    assert loaded.lookup("1.1.1.42") == (True, "vpn")
    assert loaded.lookup("2001:db8::1") == (True, "ipv6-proxy")
    assert loaded.lookup("9.9.9.9") is None
    assert loaded.value_cols == ("is_proxy", "proxy_type")
    assert (tmp_path / "DuckDbInetLpmIndex_is_proxy_proxy_type.msgpack").exists()


def test_load_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        DuckDbInetLpmIndex.load(tmp_path, value_cols=("nonexistent",))


def test_load_corrupt_file(tmp_path):
    bad = tmp_path / "DuckDbInetLpmIndex_bad.msgpack"
    bad.write_bytes(b"not valid msgpack")
    with pytest.raises(Exception):
        DuckDbInetLpmIndex.load(tmp_path, value_cols=("bad",))


def test_save_lookups_still_work(sample_index, tmp_path):
    sample_index.save(tmp_path)

    assert sample_index.lookup("1.1.1.1") == (True, "tor")
    assert sample_index.lookup("2001:db8::1") == (True, "ipv6-proxy")
    assert sample_index.lookup("9.9.9.9") is None



if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
